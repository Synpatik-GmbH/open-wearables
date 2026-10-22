from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Date, and_, cast, func, or_, select
from sqlalchemy.orm import Query, Session

from app.models import DataPointSeries, DataSource, EventRecord
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.utils.wearable_history_clock import BERLIN, recorded_night_date
from app.utils.wearable_history_ingress import WearableHistoryMetadataUnavailableError


class WearableHistoryReadRepository:
    """Baseline inputs use the current cutoff even while raw ingress remains provisional."""

    def __init__(self, db: Session, member: UUID) -> None:
        self.db = db
        self.member = member
        self.sources = {s.provider: s for s in WearableHistoryRepository().list_sources(db, member)}

    def cutoff(self, source: DataSource) -> date | None:
        provider = getattr(source.provider, "value", source.provider)
        boundary = self.sources.get(provider)
        if boundary is None and self.sources:
            raise WearableHistoryMetadataUnavailableError("Baseline source history unavailable")
        return boundary.start_date if boundary else None

    def sleep_record_ids(self) -> Query | None:
        if not self.sources:
            return None
        # Restrict before count/cursor/limit; held duplicate nights cannot crowd out inputs.
        # A sleep that is no recorded night is dated by its Berlin calendar day, as on ingress.
        night = func.coalesce(
            func.wh_night_day(EventRecord.start_datetime, EventRecord.end_datetime, EventRecord.zone_offset),
            cast(func.timezone("Europe/Berlin", EventRecord.start_datetime), Date),
        )
        return (
            self.db.query(EventRecord.id)
            .join(DataSource)
            .filter(
                DataSource.user_id == self.member,
                or_(
                    *(
                        and_(DataSource.provider == provider, night >= source.start_date)
                        for provider, source in self.sources.items()
                    )
                ),
            )
        )

    def event_eligible(self, record: EventRecord) -> bool:
        source = self.db.get(DataSource, record.data_source_id)
        if source is None:
            raise WearableHistoryMetadataUnavailableError("Baseline data source unavailable")
        first = self.cutoff(source)
        # The ingress rule (event_date, wh_read_event): only a sleep is dated by its night.
        night = (
            recorded_night_date(record.start_datetime, record.end_datetime, record.zone_offset)
            if record.category == "sleep"
            else None
        )
        day = night or record.start_datetime.astimezone(BERLIN).date()
        return first is None or day >= first

    def filter_series(
        self, rows: list[tuple[DataPointSeries, DataSource]], start: datetime, end: datetime, *, weight: bool
    ) -> list[tuple[datetime, float]]:
        if weight or not self.sources:
            return [(sample.recorded_at, float(sample.value)) for sample, _ in rows]
        nights = list(
            self.db.execute(
                select(EventRecord, DataSource)
                .join(DataSource)
                .where(
                    DataSource.user_id == self.member,
                    EventRecord.category == "sleep",
                    EventRecord.start_datetime <= end,
                    EventRecord.end_datetime >= start,
                )
            )
        )
        intervals = [
            (
                source.provider,
                record.start_datetime,
                record.end_datetime,
                recorded_night_date(record.start_datetime, record.end_datetime, record.zone_offset),
            )
            for record, source in nights
        ]
        result = []
        for sample, source in rows:
            first = self.cutoff(source)
            matching = [
                night
                for provider, onset, wake, night in intervals
                if provider == source.provider and onset <= sample.recorded_at <= wake and night is not None
            ]
            admitted = first is None or (
                any(day >= first for day in matching)
                if matching
                else sample.recorded_at.astimezone(BERLIN).date() >= first
            )
            if admitted:
                result.append((sample.recorded_at, float(sample.value)))
        return result
