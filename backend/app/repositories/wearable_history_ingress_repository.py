from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, SeriesTypeDefinition, UserConnection
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import ProviderName
from app.utils.wearable_history_clock import BERLIN, night_placement, recorded_night_date
from app.utils.wearable_history_ingress import (
    FirstNightAssociationUnavailableError,
    PreStartDataRejectedError,
    WearableHistoryMetadataUnavailableError,
)

_NIGHT_PHYSIOLOGY = frozenset(
    (
        "heart_rate",
        "heart_rate_variability_sdnn",
        "heart_rate_variability_rmssd",
        "skin_temperature",
        "body_temperature",
        "resting_heart_rate",
        "respiratory_rate",
        "oxygen_saturation",
    )
)


class WearableHistoryIngressRepository:
    """One source lock and current cutoff for each ingestion operation."""

    def persist_event[T](self, record: Any, write: Callable[[], T], *, commit: bool = False) -> T | None:
        """Isolate a flush-only item; retain earlier pending writes on permanent rejection.

        ``begin_nested`` first flushes earlier work outside this item's savepoint.
        Callbacks must not commit: the gate commits only after the item and its
        deferred source constraints have succeeded, when the caller requests it.
        """
        try:
            with self.db.begin_nested():
                # Night evidence is recorded inside the item's savepoint, so a sleep whose
                # write fails leaves no evidence that could move the start date earlier.
                if not self.filter_events([record]):
                    return None
                result = write()
                self.db.flush()
                # Check this item's deferred details/samples while its savepoint can
                # still roll it back. Keep the constraint immediate for the remaining
                # transaction; already-immediate callers are never weakened.
                self.db.execute(text("SET CONSTRAINTS wh_ingress IMMEDIATE"))
        except Exception as exc:
            # Decorated repositories wrap SQL errors in HTTPException; inspect its cause.
            if not isinstance(exc, (PreStartDataRejectedError, IntegrityError)) and not isinstance(
                exc.__cause__, IntegrityError
            ):
                raise
            if not WearableHistoryIngressRepository(self.db).filter_events([record]):
                return None
            raise
        if commit:
            self.db.commit()
        return result

    def __init__(self, db: Session) -> None:
        self.db = db
        self.history = WearableHistoryRepository()
        self.sources: dict[tuple[UUID, str], Any] = {}
        self.data_sources: dict[UUID, DataSource] = {}
        self.series_codes: dict[int, str | None] = {}
        self.sleep_ranges: dict[tuple[UUID, str], tuple[datetime, datetime, list[EventRecord]]] = {}
        self.now: datetime | None = None

    def identity(self, observation: Any) -> tuple[UUID, str] | None:
        source_id = getattr(observation, "data_source_id", None)
        if source_id:
            source = self.data_sources.get(source_id)
            if source is None:
                source = self.db.get(DataSource, source_id)
            if source is None:
                raise WearableHistoryMetadataUnavailableError("Data source is unavailable")
            self.data_sources[source_id] = source
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
        elif isinstance(provider, str) and provider not in ProviderName._value2member_map_:
            # An importer alias such as "apple_health_xml" names its provider inside the string.
            provider = ProviderName.from_source_string(provider)
        if provider == ProviderName.INTERNAL:
            return None  # Derived OW score; attributed scores already resolved their parent/source above.
        return member, getattr(provider, "value", provider)

    def source(self, identity: tuple[UUID, str]) -> Any:
        if identity not in self.sources:
            member, provider = identity
            source = self.history.get(self.db, member, provider, lock=True)
            if source is None:
                connected_at = self.db.scalar(
                    select(UserConnection.created_at).where(
                        UserConnection.user_id == member, UserConnection.provider == provider
                    )
                )
                if connected_at is not None:
                    # The connection committed but its history write did not (OAuth callback
                    # failure): begin the source from the connection's own persisted clock,
                    # exactly as the callback would have.
                    self.history.begin_source(self.db, member, provider, connected_at)
                    source = self.history.get(self.db, member, provider, lock=True)
                elif self.history.list_sources(self.db, member):
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

    def series_code(self, sample: Any) -> str | None:
        series = getattr(sample, "series_type", None)
        if series is None:
            definition_id = sample.series_type_definition_id
            if definition_id not in self.series_codes:
                definition = self.db.get(SeriesTypeDefinition, definition_id)
                self.series_codes[definition_id] = definition.code if definition else None
            return self.series_codes[definition_id]
        return getattr(series, "value", series)

    def _sleep_intervals(self, identity: tuple[UUID, str], start: datetime, end: datetime) -> list[EventRecord]:
        cached = self.sleep_ranges.get(identity)
        if cached is not None:
            if cached[0] <= start and end <= cached[1]:
                return cached[2]
            start, end = min(start, cached[0]), max(end, cached[1])
        sleeps = list(
            self.db.scalars(
                select(EventRecord)
                .join(DataSource)
                .where(
                    DataSource.user_id == identity[0],
                    DataSource.provider == identity[1],
                    EventRecord.category == "sleep",
                    EventRecord.start_datetime <= end,
                    EventRecord.end_datetime >= start,
                )
            )
        )
        self.sleep_ranges[identity] = (start, end, sleeps)
        return sleeps

    def prime_samples(self, samples: list[Any]) -> None:
        """Fetch containing intervals once per frozen source and batch, before per-sample admission."""
        ranges: dict[tuple[UUID, str], tuple[datetime, datetime]] = {}
        for sample in samples:
            if self.series_code(sample) not in _NIGHT_PHYSIOLOGY:
                continue
            identity = self.identity(sample)
            if identity is None:
                continue
            source = self.source(identity)
            if source is None or self.now < source.freeze_at:
                continue
            instant = sample.recorded_at
            lower, upper = ranges.get(identity, (instant, instant))
            ranges[identity] = (min(lower, instant), max(upper, instant))
        for identity, (start, end) in ranges.items():
            self._sleep_intervals(identity, start, end)

    def sample_admitted(self, sample: Any, intervals: list[Any] | None = None) -> bool:
        series = self.series_code(sample)
        if series == "weight":
            return True
        identity = self.identity(sample)
        if identity is None:
            return True
        source = self.source(identity)
        if source is None or self.now < source.freeze_at:
            return True
        instant = sample.recorded_at
        calendar = instant.astimezone(BERLIN).date()
        if series in _NIGHT_PHYSIOLOGY:
            sleeps = [*self._sleep_intervals(identity, instant, instant), *(intervals or [])]
            matching = [
                r for r in sleeps if r.start_datetime <= instant <= r.end_datetime and self.identity(r) == identity
            ]
            # Only recorded nights override a sample's calendar date. Daytime nap
            # intervals do not turn admitted daytime physiology into pre-start data.
            nights = [
                night
                for r in matching
                if (night := recorded_night_date(r.start_datetime, r.end_datetime, r.zone_offset)) is not None
            ]
            if any(night >= source.start_date for night in nights):
                return True
            if nights:
                return False
            if (
                not nights
                and calendar == source.start_date - timedelta(days=1)
                and night_placement(instant, getattr(sample, "zone_offset", None)) == (True, source.start_date)
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
        self.prime_samples(samples)
        return [sample for sample in samples if self.sample_admitted(sample)]
