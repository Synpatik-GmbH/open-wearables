"""2.96.6.1 (spec N9, AC12): a Polar exercise is stored at its true start instant.

Polar sends ``start_time`` as local wall-clock time with no zone, plus
``start_time_utc_offset`` in minutes. The true UTC instant is local minus offset.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from freezegun import freeze_time
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import EventRecord, PolarWorkoutTrueTime, WorkoutDetails
from app.schemas.enums import ProviderName
from app.schemas.providers.polar import ExerciseJSON as PolarExerciseJSON
from app.services.providers.polar.strategy import PolarStrategy
from tests.factories import DataSourceFactory, EventRecordFactory, UserConnectionFactory, UserFactory


def _exercise(exercise_id: str, start_time: str, offset_minutes: int, duration: str = "PT1H0M0S") -> dict:
    return {
        "id": exercise_id,
        "upload_time": "2026-10-13T09:00:00.000Z",
        "polar_user": "https://www.polaraccesslink.com/v3/users/12345",
        "device": "Polar Vantage V2",
        "start_time": start_time,
        "start_time_utc_offset": offset_minutes,
        "duration": duration,
        "sport": "RUNNING",
        "detailed_sport_info": "RUNNING",
    }


class TestPolarStartInstant:
    def test_berlin_summer_1025_is_0825_utc(self) -> None:
        workouts = PolarStrategy().workouts

        start, end = workouts._extract_dates_with_offset("2026-10-13T10:25:00", 120, "PT45M0S")

        assert start == datetime(2026, 10, 13, 8, 25, tzinfo=timezone.utc)
        assert end == datetime(2026, 10, 13, 9, 10, tzinfo=timezone.utc)

    def test_negative_offset_is_added_back(self) -> None:
        workouts = PolarStrategy().workouts

        start, _ = workouts._extract_dates_with_offset("2026-01-15T08:00:00", -300, "PT30M0S")

        assert start == datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)

    def test_a_start_time_that_already_carries_a_zone_is_not_shifted_again(self) -> None:
        workouts = PolarStrategy().workouts

        start, _ = workouts._extract_dates_with_offset("2026-10-13T10:25:00+02:00", 120, "PT45M0S")

        assert start == datetime(2026, 10, 13, 8, 25, tzinfo=timezone.utc)

    @patch("app.services.providers.templates.base_workouts.make_authenticated_request")
    def test_stored_row_holds_the_true_instant_whatever_the_session_time_zone(
        self, mock_request: MagicMock, db: Session
    ) -> None:
        # A naive datetime bound to timestamptz is read in the SESSION time zone, so the stored
        # instant must not depend on it. Berlin here would move a naive 08:25 to 06:25Z.
        db.execute(text("SET TIME ZONE 'Europe/Berlin'"))
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        mock_request.return_value = [_exercise("EX-1025", "2026-10-13T10:25:00", 120, "PT45M0S")]

        PolarStrategy().workouts.load_data(db, user.id)

        row = db.query(EventRecord).filter(EventRecord.external_id == "EX-1025").one()
        assert row.start_datetime == datetime(2026, 10, 13, 8, 25, tzinfo=timezone.utc)
        assert row.end_datetime == datetime(2026, 10, 13, 9, 10, tzinfo=timezone.utc)
        assert row.zone_offset == "+02:00"


def _now_local_iso(days_ago: int) -> str:
    """A Polar-style local start time (no zone) ``days_ago`` days back, at 10:25."""
    day = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return day.strftime("%Y-%m-%dT10:25:00")


@patch("app.services.event_record_service.EventRecordService._schedule_workout_finalize")
@patch("app.services.event_record_service.svix_service.is_enabled", return_value=True)
@patch("app.services.providers.templates.base_workouts.make_authenticated_request")
class TestPolarWorkoutSavePath:
    """2.96.6.1 (L2, AC3, AC16): what a pull writes for each exercise Polar lists."""

    def test_a_new_exercise_is_stored_and_recorded_as_true_time(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        mock_request.return_value = [_exercise("EX-NEW", _now_local_iso(3), 120)]

        saved = PolarStrategy().workouts.load_data(db, user.id)

        row = db.query(EventRecord).filter(EventRecord.external_id == "EX-NEW").one()
        assert saved == 1
        assert db.get(PolarWorkoutTrueTime, row.id).recorded_by == "ingest"
        schedule.assert_called_once_with(row.id)

    def test_an_exercise_already_stored_is_left_alone_and_sends_nothing(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        # The shifted row a pre-fix pull wrote. Its true time is two offsets earlier, so the
        # fixed arithmetic no longer collides with it on (data source, start, end): without the
        # external-id check, every hourly pull would add a second copy and a second workout.created.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        source = DataSourceFactory(user=user, provider=ProviderName.POLAR, source="polar")
        shifted_start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=3)
        shifted = EventRecordFactory(
            data_source=source, external_id="EX-OLD", start_datetime=shifted_start, zone_offset="+02:00"
        )
        mock_request.return_value = [_exercise("EX-OLD", _now_local_iso(3), 120)]

        saved = PolarStrategy().workouts.load_data(db, user.id)

        rows = db.query(EventRecord).filter(EventRecord.external_id == "EX-OLD").all()
        assert saved == 0
        assert [r.id for r in rows] == [shifted.id]
        assert rows[0].start_datetime == shifted_start
        assert db.get(PolarWorkoutTrueTime, shifted.id) is None
        schedule.assert_not_called()

    def test_a_second_pull_of_a_recorded_exercise_writes_nothing(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        mock_request.return_value = [_exercise("EX-TWICE", _now_local_iso(2), 120)]
        workouts = PolarStrategy().workouts
        workouts.load_data(db, user.id)
        schedule.reset_mock()

        saved = workouts.load_data(db, user.id)

        assert saved == 0
        assert db.query(EventRecord).filter(EventRecord.external_id == "EX-TWICE").count() == 1
        schedule.assert_not_called()

    def test_an_exercise_older_than_90_days_is_not_stored(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        mock_request.return_value = [
            _exercise("EX-91", _now_local_iso(91), 120),
            _exercise("EX-89", _now_local_iso(89), 120),
        ]

        saved = PolarStrategy().workouts.load_data(db, user.id)

        stored = {r.external_id for r in db.query(EventRecord).filter(EventRecord.external_id.like("EX-%")).all()}
        assert saved == 1
        assert stored == {"EX-89"}

    def test_an_exercise_on_day_90_in_berlin_is_stored_whatever_its_hour(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        # Codex 4274696752, spec Q5 (a date exactly 90 days old is inside): at 18:00 Berlin on
        # 14 October, 08:00 on 16 July is day 90 and inside; 23:00 on 15 July is day 91.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        mock_request.return_value = [
            _exercise("EX-DAY90", "2026-07-16T08:00:00", 120),
            _exercise("EX-DAY91", "2026-07-15T23:00:00", 120),
        ]

        with freeze_time("2026-10-14T16:00:00Z"):
            saved = PolarStrategy().workouts.load_data(db, user.id)

        stored = {r.external_id for r in db.query(EventRecord).filter(EventRecord.external_id.like("EX-DAY%")).all()}
        assert saved == 1
        assert stored == {"EX-DAY90"}

    def test_the_webhook_path_follows_the_same_rules(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        source = DataSourceFactory(user=user, provider=ProviderName.POLAR, source="polar")
        EventRecordFactory(data_source=source, external_id="EX-HOOK-OLD", zone_offset="+02:00")
        workouts = PolarStrategy().workouts

        mock_request.return_value = _exercise("EX-HOOK-OLD", _now_local_iso(1), 120)
        assert workouts.fetch_and_save_exercise(db, user.id, "/v3/exercises/EX-HOOK-OLD") == 0
        mock_request.return_value = _exercise("EX-HOOK-NEW", _now_local_iso(1), 120)
        assert workouts.fetch_and_save_exercise(db, user.id, "/v3/exercises/EX-HOOK-NEW") == 1

        new_row = db.query(EventRecord).filter(EventRecord.external_id == "EX-HOOK-NEW").one()
        assert db.get(PolarWorkoutTrueTime, new_row.id).recorded_by == "ingest"
        assert db.query(EventRecord).filter(EventRecord.external_id == "EX-HOOK-OLD").count() == 1

    def test_a_record_that_already_holds_the_times_is_not_recorded_as_ours(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        # Another record (another id) already holds this data source, start and end.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        start = datetime.now(timezone.utc).replace(hour=8, minute=25, second=0, microsecond=0) - timedelta(days=2)
        exercise = _exercise("EX-NEW", (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"), 120)
        workouts = PolarStrategy().workouts
        normalized, _ = workouts._normalize_workout(PolarExerciseJSON(**exercise), user.id)
        holder = workouts.workout_repo.create(db, normalized.model_copy(update={"id": uuid4(), "external_id": "OTHER"}))
        mock_request.return_value = [exercise]

        saved = workouts.load_data(db, user.id)

        assert saved == 0
        assert db.get(PolarWorkoutTrueTime, holder.id) is None
        schedule.assert_not_called()

    def test_a_detail_that_cannot_be_written_leaves_the_exercise_to_the_next_pull(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        db.commit()  # The member/connection predate this worker attempt in production.
        mock_request.return_value = [_exercise("EX-DETAIL", _now_local_iso(1), 120)]
        workouts = PolarStrategy().workouts

        with (
            patch(
                "app.repositories.event_record_detail_repository.EventRecordDetailRepository._build_detail",
                side_effect=RuntimeError("detail down"),
            ),
            pytest.raises(RuntimeError),
        ):
            workouts.load_data(db, user.id)
        assert db.query(EventRecord).filter(EventRecord.external_id == "EX-DETAIL").count() == 0

        assert workouts.load_data(db, user.id) == 1
        assert db.query(EventRecord).filter(EventRecord.external_id == "EX-DETAIL").count() == 1

    def test_a_worker_killed_before_the_detail_leaves_nothing_the_next_pull_skips(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        # Codex 4270734324. SystemExit is a BaseException: no ``except Exception`` cleanup runs,
        # as when the worker process is killed. Whatever had committed before the detail write is
        # what the next pull finds; a committed record without its detail would read as stored
        # and be skipped for good, never carrying a detail or a workout.created.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        db.commit()  # The member/connection predate this worker attempt in production.
        mock_request.return_value = [_exercise("EX-KILLED", _now_local_iso(1), 120)]
        workouts = PolarStrategy().workouts

        with (
            patch(
                "app.repositories.event_record_detail_repository.EventRecordDetailRepository._build_detail",
                side_effect=SystemExit(1),
            ),
            pytest.raises(SystemExit),
        ):
            workouts.load_data(db, user.id)
        db.rollback()  # the process is gone: anything it had not committed is lost
        schedule.assert_not_called()

        assert workouts.load_data(db, user.id) == 1

        row = db.query(EventRecord).filter(EventRecord.external_id == "EX-KILLED").one()
        assert db.get(WorkoutDetails, row.id) is not None
        assert db.get(PolarWorkoutTrueTime, row.id).recorded_by == "ingest"
        schedule.assert_called_once_with(row.id)

    def test_two_stored_rows_for_one_exercise_do_not_stop_the_pull(
        self, mock_request: MagicMock, svix_enabled: MagicMock, schedule: MagicMock, db: Session
    ) -> None:
        # A pre-fix duplicate (Polar revised the duration, so the end differed) must read as stored,
        # not raise and fail every other exercise in the list.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        source = DataSourceFactory(user=user, provider=ProviderName.POLAR, source="polar")
        start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=4)
        for minutes in (45, 50):
            EventRecordFactory(
                data_source=source,
                external_id="EX-DUP",
                start_datetime=start,
                end_datetime=start + timedelta(minutes=minutes),
            )
        mock_request.return_value = [
            _exercise("EX-DUP", _now_local_iso(4), 120),
            _exercise("EX-AFTER", _now_local_iso(3), 120),
        ]

        saved = PolarStrategy().workouts.load_data(db, user.id)

        assert saved == 1
        assert db.query(EventRecord).filter(EventRecord.external_id == "EX-AFTER").count() == 1
