"""2.96.6.1 (spec R2, AC18): Polar is asked for last night when the member opens the app.

Through the route Calibra calls. Polar is faked at ``_make_api_request``; storage is real.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy.orm import Session

from app.models import PolarRegistration, UserConnection
from app.schemas.auth import ConnectionStatus
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.pull_budget import (
    ON_OPEN_DAILY_CAP,
    ON_OPEN_INTERVAL,
    claim_pull,
    record_full_failure,
)
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.providers.polar.test_polar_no_lost_nights import FakePolar, _stored_polar_nights

NOW = datetime(2026, 10, 14, 5, 30, tzinfo=timezone.utc)
LAST_NIGHT = NOW.date()


@pytest.fixture
def polar() -> Iterator[FakePolar]:
    fake = FakePolar()
    with patch.object(Polar247Data, "_make_api_request", side_effect=fake):
        yield fake


def _member(db: Session, *, registered: bool | None = True, status: ConnectionStatus = ConnectionStatus.ACTIVE) -> UUID:
    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="polar", status=status)
    if registered is not None:
        db.add(PolarRegistration(user_connection_id=connection.id, registered=registered, attempts=1, updated_at=NOW))
    db.commit()
    return user.id


def _ask(client: TestClient, user_id: UUID, at: datetime = NOW) -> dict:
    with freeze_time(at):
        response = client.post(
            f"/api/v1/providers/polar/users/{user_id}/sync/last-night",
            headers={"X-Open-Wearables-API-Key": ApiKeyFactory().plain_key},
        )
    assert response.status_code == 200, response.text
    return response.json()


def test_last_night_is_fetched_and_stored_in_the_request(client: TestClient, db: Session, polar: FakePolar) -> None:
    user_id = _member(db)
    polar.nights = [LAST_NIGHT - timedelta(days=3), LAST_NIGHT]

    answer = _ask(client, user_id)

    assert answer["outcome"] == "fetched"
    assert answer["reason"] is None
    assert answer["stored"] >= 1
    assert _stored_polar_nights(db, user_id) == {LAST_NIGHT}, "only the newest night: the rest is the pull's"


def test_an_ask_is_three_calls_at_most(client: TestClient, db: Session, polar: FakePolar) -> None:
    # Spec R1: the calls an ask adds are counted against the 200 a day.
    user_id = _member(db)
    polar.nights = [LAST_NIGHT - timedelta(days=d) for d in range(5)]

    _ask(client, user_id)

    assert [e for e, _ in polar.calls] == [
        "/v3/users/sleep/available",
        f"/v3/users/sleep/{LAST_NIGHT.isoformat()}",
        "/v3/users/nightly-recharge",
    ]


def test_the_pull_interval_does_not_hold_it_back(client: TestClient, db: Session, polar: FakePolar) -> None:
    # Andrei, 14 Oct 2026: the 55-minute limit must not delay this fetch.
    user_id = _member(db)
    connection = db.query(UserConnection).filter_by(user_id=user_id).one()
    claim_pull(db, connection.id, NOW - timedelta(minutes=1))
    polar.nights = [LAST_NIGHT]

    answer = _ask(client, user_id)

    assert answer["outcome"] == "fetched"
    assert LAST_NIGHT in _stored_polar_nights(db, user_id)


def test_an_ask_does_not_hold_back_the_next_pull(client: TestClient, db: Session, polar: FakePolar) -> None:
    user_id = _member(db)
    _ask(client, user_id)
    connection = db.query(UserConnection).filter_by(user_id=user_id).one()

    assert claim_pull(db, connection.id, NOW + timedelta(minutes=1)) is True


def test_a_second_ask_inside_the_interval_is_refused_without_a_call(
    client: TestClient, db: Session, polar: FakePolar
) -> None:
    user_id = _member(db)
    _ask(client, user_id)
    polar.calls.clear()

    answer = _ask(client, user_id, NOW + ON_OPEN_INTERVAL - timedelta(seconds=1))

    assert answer == {"outcome": "refused", "reason": "too_soon", "stored": 0}
    assert polar.calls == []


def test_an_ask_one_interval_later_goes_ahead(client: TestClient, db: Session, polar: FakePolar) -> None:
    user_id = _member(db)
    _ask(client, user_id)
    polar.calls.clear()

    answer = _ask(client, user_id, NOW + ON_OPEN_INTERVAL)

    assert answer["outcome"] == "fetched"
    assert polar.calls != []


def test_asks_are_thirty_seconds_apart_at_least(client: TestClient, db: Session, polar: FakePolar) -> None:
    # The interval in seconds, not read from the constant, so a shorter one is red too.
    user_id = _member(db)
    _ask(client, user_id)

    assert _ask(client, user_id, NOW + timedelta(seconds=29))["reason"] == "too_soon"
    assert _ask(client, user_id, NOW + timedelta(seconds=30))["outcome"] == "fetched"


def test_the_daily_cap_refuses_the_next_ask_until_the_next_berlin_day(
    client: TestClient, db: Session, polar: FakePolar
) -> None:
    user_id = _member(db)
    for n in range(ON_OPEN_DAILY_CAP):
        assert _ask(client, user_id, NOW + n * ON_OPEN_INTERVAL)["outcome"] == "fetched"
    polar.calls.clear()

    refused = _ask(client, user_id, NOW + ON_OPEN_DAILY_CAP * ON_OPEN_INTERVAL)
    # 22:00 UTC is midnight in Berlin in October: a new day, a new count.
    next_day = _ask(client, user_id, datetime(2026, 10, 14, 22, 0, tzinfo=timezone.utc))

    assert refused == {"outcome": "refused", "reason": "daily_cap", "stored": 0}
    assert next_day["outcome"] == "fetched"


def test_the_day_is_berlins_not_utcs(client: TestClient, db: Session, polar: FakePolar) -> None:
    # Twelve asks from 21:44 UTC on 14 October, which is 23:44 in Berlin: the cap is reached.
    user_id = _member(db)
    start = datetime(2026, 10, 14, 21, 44, tzinfo=timezone.utc)
    for n in range(ON_OPEN_DAILY_CAP):
        assert _ask(client, user_id, start + n * ON_OPEN_INTERVAL)["outcome"] == "fetched"

    # Still the 14th in Berlin: refused.
    assert _ask(client, user_id, datetime(2026, 10, 14, 21, 59, 30, tzinfo=timezone.utc))["reason"] == "daily_cap"
    # 22:00:30 UTC is the 14th by UTC and the 15th in Berlin: a count by UTC date would refuse it.
    assert _ask(client, user_id, datetime(2026, 10, 14, 22, 0, 30, tzinfo=timezone.utc))["outcome"] == "fetched"


@pytest.mark.parametrize(
    ("registered", "status", "reason"),
    [
        (False, ConnectionStatus.ACTIVE, "registration_pending"),
        (True, ConnectionStatus.REVOKED, "not_connected"),
    ],
)
def test_polar_is_not_asked_for_a_member_it_cannot_serve(
    client: TestClient, db: Session, polar: FakePolar, registered: bool, status: ConnectionStatus, reason: str
) -> None:
    user_id = _member(db, registered=registered, status=status)

    answer = _ask(client, user_id)

    assert answer == {"outcome": "refused", "reason": reason, "stored": 0}
    assert polar.calls == []


def test_a_429_answers_failed_with_its_code_after_one_call(client: TestClient, db: Session, polar: FakePolar) -> None:
    user_id = _member(db)
    polar.rate_limited = True

    answer = _ask(client, user_id)

    assert answer == {"outcome": "failed", "reason": "HTTPException_429", "stored": 0}
    assert len(polar.calls) == 1


def test_a_recharge_failure_answers_failed_and_keeps_the_night_stored(
    client: TestClient, db: Session, polar: FakePolar
) -> None:
    user_id = _member(db)
    polar.nights = [LAST_NIGHT]
    polar.failing_endpoints = {"/v3/users/nightly-recharge"}

    answer = _ask(client, user_id)

    assert answer == {"outcome": "failed", "reason": "HTTPException_503", "stored": 1}
    assert LAST_NIGHT in _stored_polar_nights(db, user_id)


def test_a_retry_gap_on_sleep_does_not_hold_back_an_ask(client: TestClient, db: Session, polar: FakePolar) -> None:
    # Spec R2 with cause 3 (v0.6): the ask on open is not slowed by any retry gap.
    user_id = _member(db)
    connection = db.query(UserConnection).filter_by(user_id=user_id).one()
    for _ in range(3):
        record_full_failure(db, connection.id, "sleep", NOW)
    polar.nights = [LAST_NIGHT]

    answer = _ask(client, user_id)

    assert answer["outcome"] == "fetched"
    assert LAST_NIGHT in _stored_polar_nights(db, user_id)


def test_a_malformed_older_entry_still_lets_recharge_be_fetched(
    client: TestClient, db: Session, polar: FakePolar
) -> None:
    # Codex 4273701335: a sleep failure other than a 429 used to skip Nightly Recharge, where a
    # Polar night's HRV comes from, so readiness could not show within the minute (R2).
    user_id = _member(db)
    polar.extra_available = ["not an entry"]
    polar.nights = [LAST_NIGHT]

    answer = _ask(client, user_id)

    assert "/v3/users/nightly-recharge" in [e for e, _ in polar.calls]
    assert LAST_NIGHT in _stored_polar_nights(db, user_id)
    assert answer["outcome"] == "failed"
    assert answer["reason"] == "polar_sleep_listing_malformed"


def test_a_429_on_the_night_list_still_asks_nothing_more(client: TestClient, db: Session, polar: FakePolar) -> None:
    # The one sleep failure that still ends the ask: Polar has said its limit is spent.
    user_id = _member(db)
    polar.failing_endpoints = {"/v3/users/sleep/available"}
    polar.failure_status = 429

    answer = _ask(client, user_id)

    assert [e for e, _ in polar.calls] == ["/v3/users/sleep/available"]
    assert answer == {"outcome": "failed", "reason": "HTTPException_429", "stored": 0}


def test_the_route_needs_the_api_key(client: TestClient, db: Session, polar: FakePolar) -> None:
    user_id = _member(db)

    response = client.post(f"/api/v1/providers/polar/users/{user_id}/sync/last-night")

    assert response.status_code == 401
    assert polar.calls == []
