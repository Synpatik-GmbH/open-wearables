"""2.96.6.1 (spec R1, AC17, AC2): Calibra's Polar calls in a day, through the sync task.

Polar's daily limit is 5000 + 100 calls per user. With 50 Polar members that is 200 calls per
member per day, which is what AC17 holds one member to here, on a worst-case day: the hourly
pull plus the adapter's morning force-sync every 15 minutes from 02:00 to 12:00 UTC, every
night Polar lists still missing at the start, and last night arriving mid-morning.

Polar is faked at ``_make_api_request`` (24/7 data) and at the request the workouts make, and
every call is counted; storage is real.
"""

import logging
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from freezegun import freeze_time
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.sync_vendor_data_task import PULL_INTERVAL_SKIP, sync_vendor_data
from app.models import PolarRegistration, SyncRun, SyncRunDataType, UserConnection
from app.schemas.auth import ConnectionStatus
from app.schemas.providers.polar import PolarWebhookEventType
from app.schemas.sync_status import SyncStatus
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.last_night import ask_polar_for_last_night
from app.services.providers.polar.pull_budget import PULL_INTERVAL, RATE_LIMITED, WEBHOOK_HR_DAILY_CAP
from app.services.providers.polar.strategy import PolarStrategy
from app.services.sync_coordination import release_primary, try_become_primary
from app.services.sync_error_code import PULL_CLAIM_FAILED
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.polar.test_polar_no_lost_nights import FakePolar, _stored_polar_nights

# A day in Berlin summer time; every instant below is UTC.
DAY = date(2026, 10, 20)
POLAR_DAILY_CALLS_PER_MEMBER_AT_50 = (5000 + 100 * 50) // 50


def _at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def polar() -> FakePolar:
    return FakePolar()


@pytest.fixture
def member(db: Session) -> UUID:
    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="polar", status=ConnectionStatus.ACTIVE)
    connection.last_synced_at = _at(0) - timedelta(hours=1)
    db.add(PolarRegistration(user_connection_id=connection.id, registered=True, attempts=1, updated_at=_at(0)))
    db.commit()
    return user.id


@pytest.fixture
def polar_api(db: Session, polar: FakePolar) -> Iterator[FakePolar]:
    """Every Polar call the task makes goes to ``polar``, workouts included."""

    def workouts_request(**kwargs: Any) -> Any:
        polar.calls.append((kwargs["endpoint"], None))
        if polar.rate_limited:
            raise HTTPException(status_code=429, detail="Polar API error: rate limit")
        if polar.failing_all:
            raise HTTPException(status_code=polar.failure_status, detail="Polar API error: down")
        return []

    session_local = MagicMock()
    session_local.return_value.__enter__.return_value = db
    session_local.return_value.__exit__.return_value = None
    with (
        patch("app.integrations.celery.tasks.sync_vendor_data_task.SessionLocal", session_local),
        patch("app.services.sync_status_service.SessionLocal", session_local),
        patch("app.services.sync_status_service.settings.persist_live_sync_runs", True),
        patch.object(Polar247Data, "_make_api_request", side_effect=polar),
        patch(
            "app.services.providers.templates.base_workouts.make_authenticated_request",
            side_effect=workouts_request,
        ),
    ):
        yield polar


def hourly(member: UUID, at: datetime) -> dict[str, Any]:
    with freeze_time(at):
        return sync_vendor_data(str(member), providers=["polar"])


def force_sync(member: UUID, at: datetime) -> dict[str, Any]:
    """The adapter's reconcile: POST /sync/historical?days=2, which queues this."""
    with freeze_time(at):
        return sync_vendor_data(
            str(member),
            start_date=(at - timedelta(days=2)).isoformat(),
            end_date=at.isoformat(),
            providers=["polar"],
            is_historical=True,
        )


def at_connect(member: UUID, at: datetime) -> dict[str, Any]:
    """The pull the OAuth callback queues (app/api/routes/v1/oauth.py)."""
    with freeze_time(at):
        return sync_vendor_data(
            str(member),
            start_date=(at - timedelta(days=90)).isoformat(),
            end_date=at.isoformat(),
            providers=["polar"],
            is_historical=True,
            ignore_pull_interval=True,
        )


def _skipped(result: dict[str, Any]) -> bool:
    return result["providers_synced"]["polar"]["params"] == {"skipped": PULL_INTERVAL_SKIP}


@pytest.mark.usefixtures("mock_celery_app")
class TestOnePullPerInterval:
    def test_a_pull_inside_the_interval_calls_nothing_and_says_it_was_skipped(
        self, db: Session, member: UUID, polar_api: FakePolar
    ) -> None:
        hourly(member, _at(6))
        polar_api.calls.clear()
        cursor = db.query(UserConnection).filter_by(user_id=member).one().last_synced_at

        result = hourly(member, _at(6) + PULL_INTERVAL - timedelta(seconds=1))

        assert polar_api.calls == []
        assert _skipped(result)
        db.expire_all()
        assert db.query(UserConnection).filter_by(user_id=member).one().last_synced_at == cursor

    def test_a_force_sync_inside_the_interval_calls_nothing(self, member: UUID, polar_api: FakePolar) -> None:
        hourly(member, _at(6))
        polar_api.calls.clear()

        result = force_sync(member, _at(6, 15))

        assert polar_api.calls == []
        assert _skipped(result)

    def test_a_pull_one_interval_later_runs(self, member: UUID, polar_api: FakePolar) -> None:
        hourly(member, _at(6))
        polar_api.calls.clear()

        result = hourly(member, _at(6) + PULL_INTERVAL)

        assert polar_api.calls != []
        assert not _skipped(result)

    def test_the_pull_at_connect_runs_inside_the_interval(self, member: UUID, polar_api: FakePolar) -> None:
        # AC1: a member who reconnects minutes after a pull still gets the pull at connect.
        hourly(member, _at(6))
        polar_api.calls.clear()

        result = at_connect(member, _at(6, 5))

        assert polar_api.calls != []
        assert not _skipped(result)

    def test_the_pull_at_connect_restarts_the_interval(self, member: UUID, polar_api: FakePolar) -> None:
        at_connect(member, _at(6))
        polar_api.calls.clear()

        result = hourly(member, _at(6, 30))

        assert polar_api.calls == []
        assert _skipped(result)


@pytest.mark.usefixtures("mock_celery_app")
class TestRateLimit:
    def test_a_429_on_workouts_asks_for_nothing_else(self, db: Session, member: UUID, polar_api: FakePolar) -> None:
        polar_api.rate_limited = True

        force_sync(member, _at(6))

        assert polar_api.calls == [("/v3/exercises", None)]
        row = (
            db.query(SyncRunDataType)
            .join(SyncRun, SyncRunDataType.run_id == SyncRun.id)
            .filter(SyncRun.user_id == member, SyncRunDataType.data_type == "data_247")
            .one()
        )
        assert row.status == SyncStatus.FAILED
        assert row.error_code == RATE_LIMITED


def _polar_data() -> Polar247Data:
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)
    return data


def _worst_case_day(
    member: UUID, polar: FakePolar, hourly_minute: int, more: list[tuple[datetime, Any]] | None = None
) -> int:
    """One member's Polar calls over DAY. Returns the count."""
    # Every night Polar lists (its last 28 days) is missing when the day starts; last night
    # reaches Polar at 06:00, when the watch syncs.
    polar.nights = [DAY - timedelta(days=d) for d in range(1, 28)]
    polar.calls.clear()
    morning_ticks = [_at(h, m) for h in range(2, 12) for m in (0, 15, 30, 45)] + [_at(12)]
    events = sorted(
        [(_at(h, hourly_minute), hourly) for h in range(24)] + [(t, force_sync) for t in morning_ticks] + (more or []),
        key=lambda e: e[0],
    )
    for at, pull in events:
        if at >= _at(6) and DAY not in polar.nights:
            polar.nights.append(DAY)
        pull(member, at)
    return len(polar.calls)


@pytest.mark.usefixtures("mock_celery_app")
class TestADay:
    @pytest.mark.parametrize("hourly_minute", [7, 50])
    def test_one_member_stays_inside_their_share_of_polars_daily_limit_at_50_members(
        self, db: Session, member: UUID, polar_api: FakePolar, hourly_minute: int
    ) -> None:
        # AC17 (R1). Two alignments of the hourly beat against the adapter's quarter hours.
        calls = _worst_case_day(member, polar_api, hourly_minute)

        assert calls <= POLAR_DAILY_CALLS_PER_MEMBER_AT_50, calls
        assert DAY in _stored_polar_nights(db, member)

    def test_a_day_on_which_polar_refuses_every_call_stays_inside_it_too(
        self, member: UUID, polar_api: FakePolar
    ) -> None:
        polar_api.rate_limited = True

        calls = _worst_case_day(member, polar_api, hourly_minute=7)

        assert calls <= POLAR_DAILY_CALLS_PER_MEMBER_AT_50, calls

    def test_last_night_is_stored_by_the_first_pull_after_it_reaches_polar(
        self, db: Session, member: UUID, polar_api: FakePolar
    ) -> None:
        # The cost of the interval: up to one interval later than a pull every 15 minutes.
        hourly(member, _at(5, 50))
        polar_api.nights = [DAY]

        force_sync(member, _at(6))  # inside the interval: skipped
        assert DAY not in _stored_polar_nights(db, member)
        force_sync(member, _at(6, 45))

        assert DAY in _stored_polar_nights(db, member)


@pytest.mark.usefixtures("mock_celery_app")
def test_a_worst_case_day_with_the_app_opened_all_morning_stays_inside_the_limit(
    db: Session, member: UUID, polar_api: FakePolar
) -> None:
    # AC17 with AC18's asks: on the worst-case day, the member also opens the app every five
    # minutes from 04:00 to 12:00 UTC. Calibra asks only while last night is missing, but the
    # worst case is that it asks every time. The asks are capped at twelve a day of three calls.
    ask_calls: list[int] = []

    def ask(member: UUID, at: datetime) -> None:
        before = len(polar_api.calls)
        with freeze_time(at):
            ask_polar_for_last_night(db, _polar_data(), member, logging.getLogger("test"))
        ask_calls.append(len(polar_api.calls) - before)

    asks = [(_at(4) + timedelta(minutes=5 * n), ask) for n in range(97)]

    calls = _worst_case_day(member, polar_api, hourly_minute=7, more=asks)

    assert sum(1 for c in ask_calls if c > 0) == 12, "the daily cap, and nothing called on a refused ask"
    assert sum(ask_calls) <= 12 * 3
    assert calls <= POLAR_DAILY_CALLS_PER_MEMBER_AT_50, calls


@pytest.mark.usefixtures("mock_celery_app")
def test_a_worst_case_day_with_continuous_heart_rate_webhooks_all_day_stays_inside_the_limit(
    db: Session, member: UUID, polar_api: FakePolar
) -> None:
    # 2.96.8 (P7, AC11): the day above, and Polar also announces new continuous heart rate every
    # five minutes all day. Each webhook that calls Polar costs two calls (the day, the sleep
    # listing for its offsets), and only WEBHOOK_HR_DAILY_CAP of them call Polar in 24 hours.
    ask_calls: list[int] = []
    hook_calls: list[int] = []

    def ask(member: UUID, at: datetime) -> None:
        before = len(polar_api.calls)
        with freeze_time(at):
            ask_polar_for_last_night(db, _polar_data(), member, logging.getLogger("test"))
        ask_calls.append(len(polar_api.calls) - before)

    def hook(member: UUID, at: datetime) -> None:
        before = len(polar_api.calls)
        with freeze_time(at):
            _polar_data().fetch_and_save_from_webhook(
                db, member, PolarWebhookEventType.CONTINUOUS_HEART_RATE, f"/v3/users/continuous-heart-rate/{DAY}"
            )
        hook_calls.append(len(polar_api.calls) - before)

    asks = [(_at(4) + timedelta(minutes=5 * n), ask) for n in range(97)]
    hooks = [(_at(0, 2) + timedelta(minutes=5 * n), hook) for n in range(288)]

    calls = _worst_case_day(member, polar_api, hourly_minute=7, more=asks + hooks)

    assert sum(1 for c in hook_calls if c > 0) == WEBHOOK_HR_DAILY_CAP
    assert sum(hook_calls) == 2 * WEBHOOK_HR_DAILY_CAP == 18
    assert calls <= POLAR_DAILY_CALLS_PER_MEMBER_AT_50, calls


@pytest.mark.usefixtures("mock_celery_app")
def test_a_night_polar_serves_late_is_stored_within_24_hours(db: Session, member: UUID, polar_api: FakePolar) -> None:
    # AC2: a night first served 1 to 90 days after its date, stored within 24 hours of being
    # served, by hourly pulls alone. The worst case: it reaches Polar a minute after a full pull,
    # while last night, already stored, is the newest night Polar lists, so no short pull takes it.
    late = DAY - timedelta(days=5)
    polar_api.nights = [DAY]
    hourly(member, _at(0, 7))
    assert _stored_polar_nights(db, member) == {DAY}
    served = _at(0, 8)
    polar_api.nights = [DAY, late]

    stored_at = None
    for hours in range(1, 26):
        at = _at(0, 7) + timedelta(hours=hours)
        hourly(member, at)
        if late in _stored_polar_nights(db, member):
            stored_at = at
            break

    assert stored_at is not None
    # Not taken by the first short pull after it was served: this is the full pull's job.
    assert stored_at - served > timedelta(hours=1)
    assert stored_at - served <= timedelta(hours=24)


def _member_on(db: Session, polar_user_id: str) -> UUID:
    """A Polar member whose connection is the Polar account ``polar_user_id``."""
    user = UserFactory()
    connection = UserConnectionFactory(
        user=user, provider="polar", status=ConnectionStatus.ACTIVE, provider_user_id=polar_user_id
    )
    connection.last_synced_at = _at(0) - timedelta(hours=1)
    db.add(PolarRegistration(user_connection_id=connection.id, registered=True, attempts=1, updated_at=_at(0)))
    db.commit()
    return user.id


@pytest.mark.usefixtures("mock_celery_app")
def test_a_profile_that_loses_the_shared_account_lock_still_gets_its_fan_out(db: Session, polar_api: FakePolar) -> None:
    # Codex 4271871209. Two Calibra profiles connected to one Polar account. The second
    # profile's own pull finds the first pulling, and skips. The primary then queues a fan-out
    # for it; the second profile's skipped pull must not have spent the claim that fan-out needs.
    first = _member_on(db, "polar-account-1")
    second = _member_on(db, "polar-account-1")
    held, token, _ = try_become_primary("polar", "polar-account-1", first, scope="pull")
    assert held

    skipped = hourly(second, _at(6))
    assert skipped["providers_synced"]["polar"]["params"] == {"linked_account": True, "skipped": True}
    release_primary("polar", "polar-account-1", first, token, scope="pull")
    polar_api.calls.clear()

    with freeze_time(_at(6, 2)):
        sync_vendor_data(
            str(second), providers=["polar"], _skip_linked_fan_out=True, _linked_primary_user_id=str(first)
        )

    assert polar_api.calls != [], "the fan-out pulled for the second profile"


@pytest.mark.usefixtures("mock_celery_app")
def test_a_primary_whose_claim_is_refused_lets_go_of_the_shared_account(db: Session, polar_api: FakePolar) -> None:
    # The first profile pulled at 06:00; at 06:20 it takes the shared lock, is refused its claim
    # and must release the lock, or the second profile's own pull is shut out too.
    first = _member_on(db, "polar-account-2")
    second = _member_on(db, "polar-account-2")
    hourly(first, _at(6))
    skipped = hourly(first, _at(6, 20))
    assert skipped["providers_synced"]["polar"]["params"] == {"skipped": PULL_INTERVAL_SKIP}
    polar_api.calls.clear()

    hourly(second, _at(6, 21))

    assert polar_api.calls != [], "the second profile became primary and pulled"


@pytest.mark.usefixtures("mock_celery_app")
def test_a_pull_whose_claim_cannot_be_recorded_calls_nothing_and_says_so_in_the_sync_status(
    db: Session, member: UUID, polar_api: FakePolar
) -> None:
    # Codex 4271871240, Andrei's ruling: skip the pull, and record a code beyond the logs.
    with patch(
        "app.services.providers.polar.strategy.claim_pull", side_effect=OperationalError("UPDATE", {}, Exception())
    ):
        result = hourly(member, _at(6))

    assert polar_api.calls == []
    assert result["errors"] == {"polar": "OperationalError"}
    run = db.query(SyncRun).filter(SyncRun.user_id == member, SyncRun.provider == "polar").one()
    assert run.status == SyncStatus.FAILED
    assert run.error == PULL_CLAIM_FAILED


@pytest.mark.usefixtures("mock_celery_app")
def test_a_day_on_which_polar_fails_every_endpoint_stays_inside_the_limit(member: UUID, polar_api: FakePolar) -> None:
    # Codex 4271871228, spec AC4 (v0.6) and AC17: Polar answers 503 to every call all day. The
    # failed full fetches wait out growing gaps instead of being asked for on every pull.
    polar_api.failing_all = True

    calls = _worst_case_day(member, polar_api, hourly_minute=7)

    assert calls <= POLAR_DAILY_CALLS_PER_MEMBER_AT_50, calls
