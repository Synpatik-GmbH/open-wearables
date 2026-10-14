from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import FKEventRecord, str_32


class PolarWorkoutTrueTime(BaseDbModel):
    """FORK (2.96.6.1, spec L2): which Polar workouts hold their true start instant.

    Polar exercises were stored two offsets late until 2.96.6.1. Every Polar workout written
    since then is recorded here at ingest (``recorded_by = "ingest"``), and every one fixed by
    ``scripts/data_migrations/correct_polar_workout_start_times.py`` is recorded when it is
    corrected (``recorded_by = "correction"``). The correction touches only Polar workouts that
    have NO row here, which is what makes it run exactly once per workout (AC16): a second run
    finds every row recorded, and a workout ingested after the fix was recorded when it was
    written.

    Identifiers and state only (R-2026-10-13-e): no time, no health value. Deleted with its
    event record, which is deleted with the user's data source, which is deleted with the user.
    """

    __tablename__ = "polar_workout_true_time"

    event_record_id: Mapped[FKEventRecord]
    recorded_by: Mapped[str_32]
