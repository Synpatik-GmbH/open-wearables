from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, SeriesTypeDefinition, UserConnection
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import ProviderName
from app.utils.wearable_history_clock import BERLIN, night_placement, recorded_night_date
from app.utils.wearable_history_ingress import (
    FirstNightAssociationUnavailableError,
    WearableHistoryMetadataUnavailableError,
)


class WearableHistoryIngressRepository:
    """One source lock and current cutoff for each ingestion operation."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.history = WearableHistoryRepository()
        self.sources: dict[tuple[UUID, str], Any] = {}
        self.now: datetime | None = None

    def identity(self, observation: Any) -> tuple[UUID, str] | None:
        source_id = getattr(observation, "data_source_id", None)
        if source_id:
            source = self.db.get(DataSource, source_id)
            if source is None:
                raise WearableHistoryMetadataUnavailableError("Data source is unavailable")
            member = getattr(observation, "user_id", source.user_id)
            if member != source.user_id:
                raise WearableHistoryMetadataUnavailableError("Data source does not belong to member")
            return source.user_id, getattr(source.provider, "value", source.provider)
        member = getattr(observation, "user_id", None)
        record_id = getattr(observation, "event_record_id", None)
        if record_id:
            record = self.db.get(EventRecord, record_id)
            if record is not None:
                return self.identity(record)
        if member is None:
            record_id = getattr(observation, "record_id", None) or getattr(observation, "event_record_id", None)
            record = self.db.get(EventRecord, record_id) if record_id else None
            return self.identity(record) if record is not None else None
        provider = getattr(observation, "provider", None)
        if provider is None:
            provider = ProviderName.from_source_string(getattr(observation, "source", None))
        if provider == ProviderName.INTERNAL:
            return None  # Derived OW score; attributed scores already resolved their parent/source above.
        return member, getattr(provider, "value", provider)

    def source(self, identity: tuple[UUID, str]) -> Any:
        if identity not in self.sources:
            member, provider = identity
            source = self.history.get(self.db, member, provider, lock=True)
            if source is None:
                connected = self.db.scalar(
                    select(UserConnection.id).where(
                        UserConnection.user_id == member, UserConnection.provider == provider
                    )
                )
                if connected or self.history.list_sources(self.db, member):
                    raise WearableHistoryMetadataUnavailableError("Connected source history is unavailable")
            self.sources[identity] = source
            self.now = self.db.scalar(select(func.clock_timestamp()))
        return self.sources[identity]

    def observe_events(self, events: list[Any], *, flush: bool = True) -> None:
        nights: dict[tuple[UUID, str], list[date]] = {}
        for record in events:
            if getattr(record, "category", None) != "sleep":
                continue
            identity = self.identity(record)
            night = recorded_night_date(record.start_datetime, record.end_datetime, record.zone_offset)
            if identity and night is not None and self.source(identity) is not None:
                nights.setdefault(identity, []).append(night)
        for (member, provider), dates in nights.items():
            self.history.observe_nights(self.db, member, provider, dates, flush=flush)

    def event_date(self, record: Any) -> date:
        if record.category == "sleep":
            return (
                recorded_night_date(record.start_datetime, record.end_datetime, record.zone_offset)
                or record.start_datetime.astimezone(BERLIN).date()
            )
        return record.start_datetime.astimezone(BERLIN).date()

    def admits_date(self, identity: tuple[UUID, str] | None, day: date) -> bool:
        if identity is None:
            return True
        source = self.source(identity)
        return source is None or self.now < source.freeze_at or day >= source.start_date

    def event_admitted(self, record: Any) -> bool:
        if record.category not in ("sleep", "workout"):
            return True
        return self.admits_date(self.identity(record), self.event_date(record))

    def sample_admitted(self, sample: Any, intervals: list[Any] | None = None) -> bool:
        series = getattr(sample, "series_type", None)
        if series is None:
            definition = self.db.get(SeriesTypeDefinition, sample.series_type_definition_id)
            series = definition.code if definition else None
        if getattr(series, "value", series) == "weight":
            return True
        identity = self.identity(sample)
        if identity is None:
            return True
        source = self.source(identity)
        if source is None or self.now < source.freeze_at:
            return True
        instant = sample.recorded_at
        calendar = instant.astimezone(BERLIN).date()
        if getattr(series, "value", series) in (
            "heart_rate",
            "heart_rate_variability_sdnn",
            "heart_rate_variability_rmssd",
            "skin_temperature",
            "resting_heart_rate",
            "respiratory_rate",
            "oxygen_saturation",
        ):
            sleeps = list(
                self.db.scalars(
                    select(EventRecord)
                    .join(DataSource)
                    .where(
                        DataSource.user_id == identity[0],
                        DataSource.provider == identity[1],
                        EventRecord.category == "sleep",
                        EventRecord.start_datetime <= instant,
                        EventRecord.end_datetime >= instant,
                    )
                )
            )
            sleeps.extend(intervals or [])
            matching = [
                r for r in sleeps if r.start_datetime <= instant <= r.end_datetime and self.identity(r) == identity
            ]
            if any(
                (recorded_night_date(r.start_datetime, r.end_datetime, r.zone_offset) or date.min) >= source.start_date
                for r in matching
            ):
                return True
            if matching:
                return False
            if (
                not matching
                and calendar == source.start_date - timedelta(days=1)
                and night_placement(instant, None) == (True, source.start_date)
            ):
                raise FirstNightAssociationUnavailableError("First-night sleep interval has not arrived; retry")
        return calendar >= source.start_date

    def score_admitted(self, score: Any) -> bool:
        record_id = getattr(score, "event_record_id", None)
        parent = self.db.get(EventRecord, record_id) if record_id else None
        return (
            self.event_admitted(parent)
            if parent is not None
            else self.admits_date(self.identity(score), score.recorded_at.astimezone(BERLIN).date())
        )

    def filter_events(self, records: list[Any]) -> list[Any]:
        self.observe_events(records)
        return [record for record in records if self.event_admitted(record)]

    def filter_samples(self, samples: list[Any]) -> list[Any]:
        return [sample for sample in samples if self.sample_admitted(sample)]
