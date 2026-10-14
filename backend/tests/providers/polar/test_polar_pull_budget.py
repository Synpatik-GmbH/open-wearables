"""2.96.6.1 (spec R1): the stored state behind Polar's call budget, against a real database."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import PolarPullState
from app.services.providers.polar.pull_budget import (
    FULL_INTERVAL,
    PULL_INTERVAL,
    claim_pull,
    forget_full_fetch,
    full_due,
    record_full_fetch,
)
from tests.factories import UserConnectionFactory

T0 = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
TYPES = ("sleep", "daily_activity", "spo2")


class TestClaimPull:
    def test_the_first_pull_of_a_connection_is_claimed(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")

        assert claim_pull(db, connection.id, T0) is True
        assert db.get(PolarPullState, connection.id).pull_started_at == T0

    def test_a_pull_inside_the_interval_is_refused_and_records_nothing(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        claim_pull(db, connection.id, T0)

        assert claim_pull(db, connection.id, T0 + PULL_INTERVAL - timedelta(seconds=1)) is False
        db.expire_all()
        # A refused pull must not move the clock, or pulls every 15 minutes would never run.
        assert db.get(PolarPullState, connection.id).pull_started_at == T0

    def test_a_pull_exactly_one_interval_later_is_claimed(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        claim_pull(db, connection.id, T0)

        assert claim_pull(db, connection.id, T0 + PULL_INTERVAL) is True

    def test_a_pull_that_ignores_the_interval_is_claimed_and_restarts_it(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        claim_pull(db, connection.id, T0)
        later = T0 + timedelta(minutes=5)

        assert claim_pull(db, connection.id, later, ignore_interval=True) is True
        assert claim_pull(db, connection.id, later + PULL_INTERVAL - timedelta(seconds=1)) is False

    def test_one_connections_pull_does_not_hold_back_another(self, db: Session) -> None:
        first = UserConnectionFactory(provider="polar")
        second = UserConnectionFactory(provider="polar")
        claim_pull(db, first.id, T0)

        assert claim_pull(db, second.id, T0) is True


class TestFullFetch:
    def test_every_type_is_due_for_a_connection_never_fetched_in_full(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")

        assert full_due(db, connection.id, T0, TYPES) == frozenset(TYPES)

    def test_a_type_fetched_in_full_is_not_due_until_the_interval_has_passed(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        record_full_fetch(db, connection.id, "sleep", T0)

        assert "sleep" not in full_due(db, connection.id, T0 + FULL_INTERVAL - timedelta(seconds=1), TYPES)
        assert "sleep" in full_due(db, connection.id, T0 + FULL_INTERVAL, TYPES)

    def test_recording_one_type_keeps_the_others(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        record_full_fetch(db, connection.id, "sleep", T0)
        record_full_fetch(db, connection.id, "spo2", T0 + timedelta(hours=1))

        assert full_due(db, connection.id, T0 + timedelta(hours=2), TYPES) == {"daily_activity"}

    def test_a_forgotten_type_is_due_again_and_the_others_are_not(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        for data_type in TYPES:
            record_full_fetch(db, connection.id, data_type, T0)

        forget_full_fetch(db, connection.id, "sleep")

        db.expire_all()
        assert full_due(db, connection.id, T0 + timedelta(hours=1), TYPES) == {"sleep"}

    def test_recording_a_fetch_does_not_move_the_pull_clock(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        claim_pull(db, connection.id, T0)
        record_full_fetch(db, connection.id, "sleep", T0 + timedelta(minutes=3))

        db.expire_all()
        assert db.get(PolarPullState, connection.id).pull_started_at == T0

    def test_an_unreadable_stored_value_makes_the_type_due(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        db.add(PolarPullState(user_connection_id=connection.id, pull_started_at=T0, full_fetched_at={"sleep": "x"}))
        db.commit()

        assert "sleep" in full_due(db, connection.id, T0, TYPES)
