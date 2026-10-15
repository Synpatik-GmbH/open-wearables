"""2.96.6.1 (spec L2, AC15, AC16): Polar workouts stored two offsets late are corrected, each once.

Rows are written here the way a pre-fix image wrote them: the stored start is the true
instant plus twice the offset, and there is no polar_workout_true_time row.
See scripts/data_migrations/correct_polar_workout_start_times.py.
"""

import importlib.util
from contextlib import nullcontext
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, PolarWorkoutTrueTime
from app.schemas.enums import ProviderName
from tests.factories import DataSourceFactory, EventRecordFactory, UserFactory

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "correct_polar_workout_start_times.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("correct_polar_workout_start_times", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_module()
correct = script.correct_polar_workout_start_times

TRUE_START = (datetime.now(timezone.utc) - timedelta(days=5)).replace(hour=8, minute=25, second=0, microsecond=0)


def _shifted_polar_workout(
    db: Session, *, offset: str | None = "+02:00", true_start: datetime = TRUE_START
) -> EventRecord:
    """A Polar workout as the pre-fix code stored it: two offsets late."""
    user = UserFactory()
    source = DataSourceFactory(user=user, provider=ProviderName.POLAR, source="polar")
    readable = offset is not None and len(offset) == 6 and offset[0] in "+-"
    minutes = (1 if offset[0] == "+" else -1) * (int(offset[1:3]) * 60 + int(offset[4:6])) if readable else 0
    shifted = true_start + timedelta(minutes=2 * minutes)
    return EventRecordFactory(
        data_source=source,
        category="workout",
        external_id=f"EX-{user.id.hex[:8]}",
        start_datetime=shifted,
        end_datetime=shifted + timedelta(minutes=45),
        duration_seconds=45 * 60,
        zone_offset=offset,
    )


def _reload(db: Session, record_id: object) -> EventRecord:
    db.expire_all()
    return db.get(EventRecord, record_id)


class TestCorrection:
    def test_a_shifted_row_is_moved_to_its_true_instant_and_recorded(self, db: Session) -> None:
        row = _shifted_polar_workout(db)

        report = correct(db, dry_run=False)

        fixed = _reload(db, row.id)
        assert report.corrected == 1
        assert fixed.start_datetime == TRUE_START
        assert fixed.end_datetime == TRUE_START + timedelta(minutes=45)
        assert fixed.duration_seconds == 45 * 60
        assert db.get(PolarWorkoutTrueTime, row.id).recorded_by == "correction"

    def test_a_negative_offset_moves_the_row_later(self, db: Session) -> None:
        row = _shifted_polar_workout(db, offset="-05:00")

        correct(db, dry_run=False)

        assert _reload(db, row.id).start_datetime == TRUE_START

    def test_a_second_run_changes_nothing(self, db: Session) -> None:
        row = _shifted_polar_workout(db)
        correct(db, dry_run=False)

        report = correct(db, dry_run=False)

        assert report.candidates == 0
        assert report.corrected == 0
        assert _reload(db, row.id).start_datetime == TRUE_START

    def test_a_row_recorded_at_ingest_is_untouched(self, db: Session) -> None:
        row = _shifted_polar_workout(db)
        db.add(PolarWorkoutTrueTime(event_record_id=row.id, recorded_by="ingest"))
        db.commit()
        stored = row.start_datetime

        report = correct(db, dry_run=False)

        assert report.candidates == 0
        assert _reload(db, row.id).start_datetime == stored

    def test_other_providers_and_polar_sleep_are_untouched(self, db: Session) -> None:
        user = UserFactory()
        apple = DataSourceFactory(user=user, provider=ProviderName.APPLE)
        polar = DataSourceFactory(user=user, provider=ProviderName.POLAR, source="polar")
        apple_workout = EventRecordFactory(data_source=apple, category="workout", zone_offset="+02:00")
        polar_sleep = EventRecordFactory(data_source=polar, category="sleep", zone_offset="+02:00")
        before = {r.id: r.start_datetime for r in (apple_workout, polar_sleep)}

        report = correct(db, dry_run=False)

        assert report.candidates == 0
        assert {rid: _reload(db, rid).start_datetime for rid in before} == before

    def test_a_dry_run_reports_and_changes_nothing(self, db: Session) -> None:
        row = _shifted_polar_workout(db)
        stored = row.start_datetime

        report = correct(db, dry_run=True)

        assert report.candidates == 1
        assert report.corrected == 0
        assert report.stopped is False
        assert _reload(db, row.id).start_datetime == stored
        assert db.get(PolarWorkoutTrueTime, row.id) is None


class TestStopsBeforeChangingAnything:
    """The hard rule: any of these in the dry run, and the run changes nothing."""

    @pytest.mark.parametrize(
        ("setup", "count_field"),
        [
            ("null_offset", "null_zone_offset"),
            ("unreadable_offset", "unreadable_zone_offset"),
            ("older_than_90_days", "older_than_90_days"),
        ],
    )
    def test_a_stop_condition_blocks_every_row(self, db: Session, setup: str, count_field: str) -> None:
        good = _shifted_polar_workout(db)
        if setup == "null_offset":
            _shifted_polar_workout(db, offset=None)
        elif setup == "unreadable_offset":
            _shifted_polar_workout(db, offset="Z")
        else:
            _shifted_polar_workout(db, true_start=datetime.now(timezone.utc) - timedelta(days=91))
        stored = good.start_datetime

        report = correct(db, dry_run=False)

        assert report.stopped is True
        assert getattr(report, count_field) == 1
        assert report.corrected == 0
        assert _reload(db, good.id).start_datetime == stored
        assert db.query(PolarWorkoutTrueTime).count() == 0

    def test_a_row_whose_true_start_is_on_day_90_in_berlin_is_inside_the_window(self, db: Session) -> None:
        # Codex 4274696752, spec Q5: the window counts Berlin dates, and day 90 is inside it at
        # any hour. A true start one second after Berlin midnight of day 90 used to count as
        # older than 90 days at any later time of the day, and stopped the run.
        berlin = ZoneInfo("Europe/Berlin")
        day90 = datetime.now(berlin).date() - timedelta(days=90)
        start = datetime.combine(day90, time(0, 0, 1), tzinfo=berlin).astimezone(timezone.utc)
        row = _shifted_polar_workout(db, true_start=start)

        report = correct(db, dry_run=False)

        assert report.older_than_90_days == 0
        assert report.stopped is False
        assert _reload(db, row.id).start_datetime == start

    def test_a_row_whose_true_start_is_on_day_91_in_berlin_stops(self, db: Session) -> None:
        berlin = ZoneInfo("Europe/Berlin")
        day91 = datetime.now(berlin).date() - timedelta(days=91)
        _shifted_polar_workout(
            db, true_start=datetime.combine(day91, time(23, 59), tzinfo=berlin).astimezone(timezone.utc)
        )

        report = correct(db, dry_run=True)

        assert report.older_than_90_days == 1
        assert report.stopped is True

    def test_a_corrected_time_that_collides_with_a_stored_row_stops(self, db: Session) -> None:
        # The other row already holds the true times and is recorded, so it is not a candidate
        # itself: the only thing that can stop this run is the collision.
        row = _shifted_polar_workout(db)
        holder = EventRecordFactory(
            data_source=db.get(DataSource, row.data_source_id),
            category="workout",
            start_datetime=TRUE_START,
            end_datetime=TRUE_START + timedelta(minutes=45),
            zone_offset="+02:00",
        )
        db.add(PolarWorkoutTrueTime(event_record_id=holder.id, recorded_by="ingest"))
        db.commit()

        report = correct(db, dry_run=False)

        assert report.candidates == 1
        assert report.null_zone_offset == 0
        assert report.stopped is True
        assert report.collisions == 1
        assert report.corrected == 0

    def test_two_candidates_that_correct_to_the_same_times_stop_the_run(self, db: Session) -> None:
        # Codex 4270734395. Neither target is held by a row today (each candidate sits at a
        # different shifted time), so only a check across the candidates sees that applying both
        # would put two rows on one (data source, start, end), which the unique index refuses.
        two_hours_late = _shifted_polar_workout(db, offset="+01:00")
        four_hours_late = EventRecordFactory(
            data_source=db.get(DataSource, two_hours_late.data_source_id),
            category="workout",
            external_id="EX-OTHER",
            start_datetime=TRUE_START + timedelta(hours=4),
            end_datetime=TRUE_START + timedelta(hours=4, minutes=45),
            duration_seconds=45 * 60,
            zone_offset="+02:00",
        )
        db.commit()
        stored = {r.id: (r.start_datetime, r.end_datetime) for r in (two_hours_late, four_hours_late)}

        report = correct(db, dry_run=False)

        assert report.candidates == 2
        assert report.stopped is True
        assert report.collisions >= 1
        assert report.corrected == 0
        for record_id, (start, end) in stored.items():
            row = _reload(db, record_id)
            assert (row.start_datetime, row.end_datetime) == (start, end)
        assert db.query(PolarWorkoutTrueTime).count() == 0

        with patch.object(script, "SessionLocal", return_value=nullcontext(db)):
            assert script.main(dry_run=True) == 2
            assert script.main(dry_run=False) == 2

    def test_a_session_time_zone_other_than_utc_stops(self, db: Session) -> None:
        row = _shifted_polar_workout(db)
        stored = row.start_datetime
        db.execute(text("SET TIME ZONE 'Europe/Berlin'"))

        report = correct(db, dry_run=False)

        assert report.stopped is True
        assert report.session_time_zone == "Europe/Berlin"
        assert _reload(db, row.id).start_datetime == stored
