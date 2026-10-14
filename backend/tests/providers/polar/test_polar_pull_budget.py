"""2.96.6.1 (spec R1): the stored state behind Polar's call budget, against a real database."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import PolarPullState
from app.services.providers.polar.pull_budget import (
    FULL_INTERVAL,
    ON_OPEN_INTERVAL,
    PULL_INTERVAL,
    TOO_SOON,
    claim_on_open,
    claim_pull,
    full_due,
    record_full_failure,
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


class TestClaimOnOpen:
    """Spec R2: asks for last night on open are limited on their own, apart from the pull interval."""

    def test_a_pull_is_claimed_on_a_row_an_ask_on_open_made(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        assert claim_on_open(db, connection.id, T0) is None

        assert claim_pull(db, connection.id, T0) is True

    def test_an_ask_on_open_is_claimed_right_after_a_pull(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        claim_pull(db, connection.id, T0)

        assert claim_on_open(db, connection.id, T0 + timedelta(seconds=1)) is None

    def test_a_refused_ask_records_nothing(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        claim_on_open(db, connection.id, T0)

        assert claim_on_open(db, connection.id, T0 + ON_OPEN_INTERVAL - timedelta(seconds=1)) == TOO_SOON
        db.expire_all()
        state = db.get(PolarPullState, connection.id)
        assert (state.on_open_at, state.on_open_count) == (T0, 1)


class TestFullFetch:
    def test_a_fetch_recorded_before_any_pull_starts_no_pull_clock(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        record_full_fetch(db, connection.id, "sleep", T0)

        assert claim_pull(db, connection.id, T0 + timedelta(minutes=1)) is True

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


class TestRetryGaps:
    """Spec cause 3, AC4 (v0.6): a failed full fetch waits a gap that doubles, up to 21 hours.

    Times are written out, not read from the constants, so a shorter or longer gap is red too.
    """

    def _due(self, db: Session, connection_id, at) -> bool:  # noqa: ANN001
        db.expire_all()
        return "spo2" in full_due(db, connection_id, at, TYPES)

    def test_the_gaps_are_one_two_four_hours_and_so_on(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        at = T0
        for gap_hours in (1, 2, 4, 8, 16):
            record_full_failure(db, connection.id, "spo2", at)
            assert not self._due(db, connection.id, at + timedelta(hours=gap_hours) - timedelta(seconds=1))
            assert self._due(db, connection.id, at + timedelta(hours=gap_hours))
            at += timedelta(hours=gap_hours)

    def test_the_gap_stops_growing_at_21_hours(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        for _ in range(8):
            record_full_failure(db, connection.id, "spo2", T0)

        assert not self._due(db, connection.id, T0 + timedelta(hours=21) - timedelta(seconds=1))
        assert self._due(db, connection.id, T0 + timedelta(hours=21))

    def test_a_success_ends_the_gaps_and_the_next_failure_starts_again_at_an_hour(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        for _ in range(4):
            record_full_failure(db, connection.id, "spo2", T0)
        record_full_fetch(db, connection.id, "spo2", T0 + timedelta(hours=9))
        later = T0 + timedelta(hours=9) + FULL_INTERVAL

        assert self._due(db, connection.id, later)
        record_full_failure(db, connection.id, "spo2", later)
        assert self._due(db, connection.id, later + timedelta(hours=1))

    def test_a_gap_holds_back_only_its_own_type(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        record_full_failure(db, connection.id, "spo2", T0)

        db.expire_all()
        assert full_due(db, connection.id, T0 + timedelta(minutes=30), TYPES) == {"sleep", "daily_activity"}

    def test_an_unreadable_gap_holds_nothing_back(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        db.add(PolarPullState(user_connection_id=connection.id, full_retry={"spo2": {"next": "x"}}))
        db.commit()

        assert self._due(db, connection.id, T0)

    def test_a_failure_does_not_move_the_pull_clock(self, db: Session) -> None:
        connection = UserConnectionFactory(provider="polar")
        claim_pull(db, connection.id, T0)
        record_full_failure(db, connection.id, "spo2", T0 + timedelta(minutes=3))

        db.expire_all()
        assert db.get(PolarPullState, connection.id).pull_started_at == T0


class TestFullFetchAgain:
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
