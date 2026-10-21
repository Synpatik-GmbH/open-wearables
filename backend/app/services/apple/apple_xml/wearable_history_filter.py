"""2.96.9.2 D-04: after the Apple source's freeze, an XML import skips every pre-start record.

Before the freeze the first night counts whole on every path (R2, S11), so after it the
first night is already stored and an XML copy would only duplicate it. Every record dated
before the start date is therefore skipped, the first night's evening included (Andrei's
accepted exception to R2 for XML arriving after the freeze, 21 Oct 2026). Weight is never
skipped (R3).
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import SeriesType
from app.utils.wearable_history_clock import BERLIN, recorded_night_date


def _berlin_date(instant: datetime) -> date:
    return instant.astimezone(BERLIN).date()


@dataclass
class PrestartSkipper:
    """``start_date`` is None while the source is provisional: then nothing is skipped."""

    start_date: date | None
    skipped: int = 0

    @classmethod
    def for_member(cls, db: Session, user_id: str) -> "PrestartSkipper":
        source = WearableHistoryRepository().get(db, UUID(user_id), "apple")
        if source is None:
            return cls(None)
        now = db.scalar(select(func.clock_timestamp()))
        return cls(source.start_date if now is not None and now >= source.freeze_at else None)

    def _prestart(self, day: date) -> bool:
        if self.start_date is None or day >= self.start_date:
            return False
        self.skipped += 1
        return True

    def samples(self, samples: list[Any]) -> list[Any]:
        return [
            s for s in samples if s.series_type == SeriesType.weight or not self._prestart(_berlin_date(s.recorded_at))
        ]

    def workouts(self, workouts: list[tuple[Any, Any]]) -> list[tuple[Any, Any]]:
        return [(r, d) for r, d in workouts if not self._prestart(_berlin_date(r.start_datetime))]

    def sleep(self, sync_request: Any) -> Any:
        if sync_request is None or not sync_request.data.sleep:
            return sync_request
        kept = [
            record
            for record in sync_request.data.sleep
            if not self._prestart(
                recorded_night_date(record.startDate, record.endDate, record.zoneOffset)
                or _berlin_date(record.startDate)
            )
        ]
        return sync_request.model_copy(update={"data": sync_request.data.model_copy(update={"sleep": kept})})

    def rejected_sleep_batch(self, batch_size: int) -> bool:
        """The sleep handler's own pre-start rejection, counted as skipped once frozen."""
        if self.start_date is None:
            return False
        self.skipped += batch_size
        return True
