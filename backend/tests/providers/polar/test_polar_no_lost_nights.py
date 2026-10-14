"""2.96.6.1, No lost wearable data: what a Polar pull asks for, and what a failure leaves behind.

Spec causes 1-3 and 8 (AC1-AC5, AC11). Polar is faked at ``_make_api_request``; storage is real.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, HealthScore, User, UserConnection
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.schemas.providers.polar.webhook import PolarWebhookEventType
from app.schemas.sync_status import DataTypeFailure
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.strategy import PolarStrategy
from tests.factories import DataSourceFactory, EventRecordFactory, UserConnectionFactory, UserFactory

TODAY = datetime.now(timezone.utc).date()


def _night(day: date) -> dict[str, str]:
    """What /sleep/available lists for a night that ended on ``day``."""
    return {
        "date": day.isoformat(),
        "start_time": f"{(day - timedelta(days=1)).isoformat()}T23:00:00+02:00",
        "end_time": f"{day.isoformat()}T07:00:00+02:00",
    }


def _sleep(day: date) -> dict[str, Any]:
    night = _night(day)
    return {
        "date": night["date"],
        "sleep_start_time": night["start_time"],
        "sleep_end_time": night["end_time"],
        "light_sleep": 14400,
        "deep_sleep": 5400,
        "rem_sleep": 7200,
    }


class FakePolar:
    """Answers Polar endpoints from a table and records every request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.nights: list[date] = []
        self.failing_nights: set[date] = set()
        self.recharges: list[dict[str, Any]] = []
        self.failing_endpoints: set[str] = set()
        # What a failing endpoint or night answers; 429 is Polar's rate limit (spec R1).
        self.failure_status = 503
        # Every endpoint answers 429 (Polar's daily limit is spent).
        self.rate_limited = False
        # The sleep score each night carries, when set.
        self.sleep_score: int | None = None

    def __call__(
        self, db: Session, user_id: UUID, endpoint: str, params: dict | None = None, headers: dict | None = None
    ) -> Any:  # noqa: ARG002
        self.calls.append((endpoint, params))
        if self.rate_limited:
            raise HTTPException(status_code=429, detail="Polar API error: rate limit")
        if endpoint in self.failing_endpoints:
            raise HTTPException(
                status_code=self.failure_status, detail="upstream said something with the user's data in it"
            )
        if endpoint == "/v3/users/sleep/available":
            return {"available": [_night(d) for d in self.nights]}
        if endpoint.startswith("/v3/users/sleep/"):
            day = date.fromisoformat(endpoint.rsplit("/", 1)[1])
            if day in self.failing_nights:
                raise HTTPException(status_code=self.failure_status, detail="night failed")
            sleep = _sleep(day)
            if self.sleep_score is not None:
                sleep["sleep_score"] = self.sleep_score
            return sleep
        if endpoint == "/v3/users/nightly-recharge":
            return {"recharges": self.recharges}
        return [] if endpoint != "/v3/users/continuous-heart-rate" else {"heart_rates": []}

    def requested(self, endpoint: str) -> list[dict[str, Any] | None]:
        return [p for e, p in self.calls if e == endpoint]


@pytest.fixture
def polar() -> FakePolar:
    return FakePolar()


@pytest.fixture
def data_247(polar: FakePolar) -> Polar247Data:
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)
    with patch.object(Polar247Data, "_make_api_request", side_effect=polar):
        yield data


def _polar_user(db: Session) -> UUID:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="polar")
    db.commit()
    return user.id


def _stored_polar_nights(db: Session, user_id: UUID) -> set[date]:
    rows = (
        db.query(EventRecord)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            DataSource.provider == ProviderName.POLAR,
            EventRecord.category == "sleep",
        )
        .all()
    )
    return {r.end_datetime.astimezone(timezone.utc).date() for r in rows}


class TestWindows:
    """Every pull asks each endpoint for the window Polar serves, whatever the cursor says."""

    def test_a_live_pull_with_a_fresh_cursor_still_asks_for_the_served_window(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _polar_user(db)
        an_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)

        data_247.load_and_save_all(db, user_id, start_time=an_hour_ago, end_time=datetime.now(timezone.utc))

        activity_from = [date.fromisoformat(p["from"]) for p in polar.requested("/v3/users/activities")]
        assert min(activity_from) == TODAY - timedelta(days=90)
        for endpoint in (
            "/v3/users/continuous-heart-rate",
            "/v3/users/cardio-load/date",
            "/v3/users/sleepwise/alertness",
            "/v3/users/sleepwise/circadian-bedtime",
            "/v3/users/body-temperature",
            "/v3/users/sleep-skin-temperature",
            "/v3/users/spo2",
            "/v3/users/wrist-ecg",
        ):
            (params,) = polar.requested(endpoint)
            assert date.fromisoformat(params["from"]) == TODAY - timedelta(days=27), endpoint

    def test_no_single_range_request_spans_more_than_28_days(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # Polar rejects a from/to range longer than 28 days on these endpoints.
        user_id = _polar_user(db)

        data_247.load_and_save_all(db, user_id)

        for endpoint, params in polar.calls:
            if params and "from" in params:
                span = date.fromisoformat(params["to"]) - date.fromisoformat(params["from"])
                assert span <= timedelta(days=27), (endpoint, params)


_SCORE_WRITE = "app.services.providers.polar.data_247.health_score_service.bulk_create"


def _sleep_scores(db: Session, user_id: UUID) -> list[float]:
    rows = (
        db.query(HealthScore)
        .filter(HealthScore.user_id == user_id, HealthScore.category == HealthScoreCategory.SLEEP)
        .all()
    )
    return [float(r.value) for r in rows]


class TestNights:
    def test_every_listed_night_inside_90_days_that_is_not_stored_is_fetched(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # AC1/AC2: last night and a night Polar served late are both fetched on this pull.
        # AC3: a night 91 days old is not.
        user_id = _polar_user(db)
        polar.nights = [TODAY, TODAY - timedelta(days=10), TODAY - timedelta(days=91)]

        result = data_247.load_and_save_all(db, user_id)

        assert result["sleep"] == 2
        assert _stored_polar_nights(db, user_id) == {TODAY, TODAY - timedelta(days=10)}
        assert f"/v3/users/sleep/{(TODAY - timedelta(days=91)).isoformat()}" not in [e for e, _ in polar.calls]

    def test_a_night_already_stored_is_not_fetched_again(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _polar_user(db)
        polar.nights = [TODAY]
        data_247.load_and_save_all(db, user_id)
        polar.calls.clear()

        data_247.load_and_save_all(db, user_id)

        assert [e for e, _ in polar.calls if e.startswith("/v3/users/sleep/2")] == []

    @pytest.mark.parametrize(("provider", "category"), [(ProviderName.APPLE, "sleep"), (ProviderName.POLAR, "workout")])
    def test_only_a_polar_sleep_counts_as_the_night_stored(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, provider: ProviderName, category: str
    ) -> None:
        # An Apple night, or a Polar workout, over the same hours is not this Polar night.
        user_id = _polar_user(db)
        night = _night(TODAY)
        source = DataSourceFactory(user=db.get(User, user_id), provider=provider)
        EventRecordFactory(
            data_source=source,
            category=category,
            start_datetime=datetime.fromisoformat(night["start_time"]),
            end_datetime=datetime.fromisoformat(night["end_time"]),
        )
        polar.nights = [TODAY]

        data_247.load_and_save_all(db, user_id)

        assert f"/v3/users/sleep/{TODAY.isoformat()}" in [e for e, _ in polar.calls]

    def test_a_night_whose_heart_rate_fails_to_save_is_fetched_again(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # The heart rate is saved before the sleep record, so a failure there leaves the night
        # missing rather than stored without it.
        user_id = _polar_user(db)
        polar.nights = [TODAY]
        with patch.object(Polar247Data, "_save_timeseries", side_effect=RuntimeError("down")):
            data_247.load_and_save_all(db, user_id)
        assert _stored_polar_nights(db, user_id) == set()

        polar.calls.clear()
        data_247.load_and_save_all(db, user_id)

        assert f"/v3/users/sleep/{TODAY.isoformat()}" in [e for e, _ in polar.calls]
        assert _stored_polar_nights(db, user_id) == {TODAY}

    def test_a_night_whose_score_fails_to_save_is_fetched_again_and_keeps_its_score(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # Codex 4270734341. The score is saved before the sleep record commits, so a failed score
        # write leaves the night missing (asked for again) rather than stored without its score.
        user_id = _polar_user(db)
        polar.nights = [TODAY]
        polar.sleep_score = 82
        with patch(_SCORE_WRITE, side_effect=RuntimeError("down")):
            first = data_247.load_and_save_all(db, user_id)
        assert isinstance(first["sleep"], DataTypeFailure)
        assert _stored_polar_nights(db, user_id) == set()

        polar.calls.clear()
        second = data_247.load_and_save_all(db, user_id)

        assert second["sleep"] == 1
        assert f"/v3/users/sleep/{TODAY.isoformat()}" in [e for e, _ in polar.calls]
        assert _stored_polar_nights(db, user_id) == {TODAY}
        assert _sleep_scores(db, user_id) == [82]

    def test_a_night_merged_into_an_adjacent_one_keeps_its_score_on_the_merged_record(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # The merge stores the night under a new record id, not the one normalize_sleep minted;
        # the score must point at the record that exists, or its foreign key refuses it.
        user_id = _polar_user(db)
        night = _night(TODAY)
        source = DataSourceFactory(user=db.get(User, user_id), provider=ProviderName.POLAR)
        nap_end = datetime.fromisoformat(night["start_time"]) - timedelta(hours=1)
        EventRecordFactory(
            data_source=source,
            category="sleep",
            type="sleep_session",
            start_datetime=nap_end - timedelta(hours=1),
            end_datetime=nap_end,
        )
        polar.nights = [TODAY]
        polar.sleep_score = 64

        result = data_247.load_and_save_all(db, user_id)

        assert result["sleep"] == 1
        sleep = (
            db.query(EventRecord)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(DataSource.user_id == user_id, EventRecord.category == "sleep")
            .one()
        )
        score = (
            db.query(HealthScore)
            .filter(HealthScore.user_id == user_id, HealthScore.provider == ProviderName.POLAR)
            .one()
        )
        assert score.event_record_id == sleep.id
        assert float(score.value) == 64

    def test_a_failed_night_does_not_lose_the_others_and_is_fetched_again(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # AC4: the failed night is asked for on the next pull; the good one is kept now.
        user_id = _polar_user(db)
        bad, good = TODAY - timedelta(days=1), TODAY
        polar.nights = [bad, good]
        polar.failing_nights = {bad}

        first = data_247.load_and_save_all(db, user_id)

        assert first["sleep"] == DataTypeFailure(code="HTTPException_503", saved=1)
        assert _stored_polar_nights(db, user_id) == {good}

        polar.failing_nights = set()
        polar.calls.clear()
        second = data_247.load_and_save_all(db, user_id)

        assert second["sleep"] == 1
        refetched = [e for e, _ in polar.calls if e.startswith("/v3/users/sleep/2")]
        assert refetched == [f"/v3/users/sleep/{bad.isoformat()}"]
        assert _stored_polar_nights(db, user_id) == {good, bad}


class TestRecharge:
    def test_recharge_is_requested_on_every_pull_and_a_date_that_appears_late_is_stored(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # AC11: Recharge has no webhook, so every pull asks for it.
        user_id = _polar_user(db)
        late = {"date": (TODAY - timedelta(days=5)).isoformat(), "nightly_recharge_status": 3, "heart_rate_avg": 55}
        data_247.load_and_save_all(db, user_id)
        polar.recharges = [late]

        second = data_247.load_and_save_all(db, user_id)

        assert len(polar.requested("/v3/users/nightly-recharge")) == 2
        assert second["nightly_recharge"] == 1

    def test_recharge_older_than_90_days_is_not_stored(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _polar_user(db)
        polar.recharges = [
            {"date": (TODAY - timedelta(days=91)).isoformat(), "nightly_recharge_status": 3},
            {"date": (TODAY - timedelta(days=2)).isoformat(), "nightly_recharge_status": 3},
        ]

        result = data_247.load_and_save_all(db, user_id)

        assert result["nightly_recharge"] == 1


class _SyncThread:
    def __init__(self, target: Any, args: tuple, daemon: bool) -> None:  # noqa: ARG002
        self._target, self._args = target, args

    def start(self) -> None:
        self._target(*self._args)


class TestNoRepeatWebhooks:
    def test_a_second_pull_of_unchanged_recharge_sends_no_series_webhook(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # Every pull re-reads Polar's whole served window; what is already stored goes nowhere.
        user_id = _polar_user(db)
        polar.recharges = [
            {"date": (TODAY - timedelta(days=3)).isoformat(), "nightly_recharge_status": 3, "heart_rate_avg": 55}
        ]
        emitted: list[dict] = []
        with (
            patch("app.services.timeseries_service.svix_service.is_enabled", return_value=True),
            patch("app.services.timeseries_service.threading.Thread", _SyncThread),
            patch(
                "app.services.timeseries_service.on_timeseries_batch_saved",
                side_effect=lambda **kw: emitted.append(kw),
            ),
        ):
            data_247.load_and_save_all(db, user_id)
            first = len(emitted)
            data_247.load_and_save_all(db, user_id)

        assert first == 1
        assert len(emitted) == first


class TestFailedTypes:
    def test_a_type_that_fails_is_reported_failed_with_a_code_and_the_rest_still_run(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _polar_user(db)
        polar.failing_endpoints = {"/v3/users/nightly-recharge"}
        polar.nights = [TODAY]

        result = data_247.load_and_save_all(db, user_id)

        assert result["nightly_recharge"] == DataTypeFailure(code="HTTPException_503")
        assert result["sleep"] == 1


class TestTheWebhookPath:
    def test_a_webhook_for_a_night_already_stored_puts_its_score_on_the_stored_record(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # The night lands in the record already there (no new record), and its score points at it.
        user_id = _polar_user(db)
        polar.nights = [TODAY]
        data_247.load_and_save_all(db, user_id)  # stored without a score
        polar.sleep_score = 70

        result = data_247.fetch_and_save_from_webhook(
            db, user_id, PolarWebhookEventType.SLEEP, f"/v3/users/sleep/{TODAY.isoformat()}"
        )

        assert result == {"sleep": 1}
        sleep = (
            db.query(EventRecord)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(DataSource.user_id == user_id, EventRecord.category == "sleep")
            .one()
        )
        score = (
            db.query(HealthScore)
            .filter(HealthScore.user_id == user_id, HealthScore.provider == ProviderName.POLAR)
            .one()
        )
        assert score.event_record_id == sleep.id

    def test_a_webhook_night_whose_score_fails_to_save_is_fetched_by_the_next_pull_with_its_score(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # Codex 4270734341, the webhook's SLEEP case: same order as the pull, so a failed score
        # write leaves the night missing, not stored without its score.
        user_id = _polar_user(db)
        polar.nights = [TODAY]
        polar.sleep_score = 77
        with patch(_SCORE_WRITE, side_effect=RuntimeError("down")), pytest.raises(RuntimeError):
            data_247.fetch_and_save_from_webhook(
                db, user_id, PolarWebhookEventType.SLEEP, f"/v3/users/sleep/{TODAY.isoformat()}"
            )
        db.rollback()  # the webhook's session ends with the failure
        assert _stored_polar_nights(db, user_id) == set()

        polar.calls.clear()
        data_247.load_and_save_all(db, user_id)

        assert f"/v3/users/sleep/{TODAY.isoformat()}" in [e for e, _ in polar.calls]
        assert _stored_polar_nights(db, user_id) == {TODAY}
        assert _sleep_scores(db, user_id) == [77]

    def test_a_failed_webhook_fetch_does_not_mark_the_connection_synced_and_reports_a_code(self, db: Session) -> None:
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user, provider="polar", provider_user_id="4711", status=ConnectionStatus.ACTIVE
        )
        before = connection.last_synced_at
        payload = {
            "event": "SLEEP",
            "user_id": 4711,
            "url": f"https://www.polaraccesslink.com/v3/users/sleep/{TODAY.isoformat()}",
        }
        failing = HTTPException(status_code=503, detail="upstream text")

        with patch.object(Polar247Data, "_make_api_request", side_effect=failing):
            result = PolarStrategy().webhooks.process_payload(db, payload, trace_id="t")

        db.expire_all()
        assert db.get(UserConnection, connection.id).last_synced_at == before
        assert result["status"] == "error"
        assert result["error"] == "HTTPException_503"
