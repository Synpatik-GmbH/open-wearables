from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.wearable_history_source import WearableHistorySource
from app.utils.wearable_history_clock import BERLIN, candidate_start, freeze_at


class WearableHistoryRepository:
    def raw_archive_allowed(self, db: Session, user_id: UUID | None, provider: str) -> bool:
        provider = "apple" if provider == "apple_xml" else provider
        statement = select(WearableHistorySource.id).where(
            WearableHistorySource.provider == provider, WearableHistorySource.freeze_at <= func.clock_timestamp()
        )
        if user_id is not None:
            statement = statement.where(WearableHistorySource.user_id == user_id)
        return db.scalar(statement.limit(1)) is None

    def begin_source(
        self, db: Session, user_id: UUID, provider: str, connected_at: datetime | None = None
    ) -> WearableHistorySource:
        connected = connected_at or db.scalar(select(func.clock_timestamp()))
        if connected is None:
            raise RuntimeError("Wearable connection clock unavailable")
        day = connected.astimezone(BERLIN).date()
        db.execute(
            insert(WearableHistorySource)
            .values(
                id=uuid4(),
                user_id=user_id,
                provider=provider,
                connection_day=day,
                start_date=day,
                freeze_at=freeze_at(day),
                night_dates=[],
            )
            .on_conflict_do_nothing(constraint="uq_wearable_history_user_provider")
        )
        source = self.get(db, user_id, provider)
        if source is None:
            raise RuntimeError("Source creation did not persist")
        return source

    def get(self, db: Session, user_id: UUID, provider: str, *, lock: bool = False) -> WearableHistorySource | None:
        statement = select(WearableHistorySource).where(
            WearableHistorySource.user_id == user_id, WearableHistorySource.provider == provider
        )
        if lock:
            statement = statement.with_for_update()
        source = db.scalar(statement)
        if lock and source is not None and not db.is_modified(source, include_collections=True):
            db.refresh(source, with_for_update=True)
        return source

    def list_sources(self, db: Session, user_id: UUID) -> list[WearableHistorySource]:
        return list(
            db.scalars(
                select(WearableHistorySource)
                .where(WearableHistorySource.user_id == user_id)
                .order_by(WearableHistorySource.provider)
            )
        )

    def observe_nights(
        self,
        db: Session,
        user_id: UUID,
        provider: str,
        nights: list[date],
        now: datetime | None = None,
        *,
        flush: bool = True,
    ) -> WearableHistorySource | None:
        source = self.get(db, user_id, provider, lock=True)
        if source is None:
            return None
        instant = now or db.scalar(select(func.clock_timestamp()))
        if instant is None:
            raise RuntimeError("Wearable history clock unavailable")
        if instant.astimezone(UTC) >= source.freeze_at.astimezone(UTC):
            return source
        first = source.connection_day - timedelta(days=28)
        source.night_dates = sorted({d for d in [*source.night_dates, *nights] if first <= d <= source.connection_day})
        source.start_date = min(source.start_date, candidate_start(source.connection_day, source.night_dates))
        if flush:
            db.flush()
        return source
