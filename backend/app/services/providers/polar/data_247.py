import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.constants.series_types.polar import (
    ANS_CHARGE_STATUS_LABELS,
    BODY_TEMP_SERIES_TYPE,
    CIRCADIAN_QUALITY_LABELS,
    CIRCADIAN_QUALITY_VALUES,
    GRADE_CLASSIFICATION_LABELS,
    HYPNOGRAM_STAGE_MAP,
    NIGHTLY_RECHARGE_STATUS_LABELS,
    SLEEP_INERTIA_LABELS,
)
from app.database import DbSession
from app.models import DataSource, EventRecord
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import HealthScoreCategory, ProviderName, SeriesType
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    HealthScoreCreate,
    ScoreComponent,
    SleepStage,
    TimeSeriesSampleCreate,
)
from app.schemas.providers.polar import (
    CardioLoadJSON,
    ContinuousHeartRateJSON,
    DailyActivityJSON,
    NightlyRechargeJSON,
    PolarWebhookEventType,
    SleepJSON,
)
from app.schemas.providers.polar.elixir import (
    BodyTemperaturePeriodJSON,
    EcgTestResultJSON,
    SkinTemperatureJSON,
    Spo2TestResultJSON,
    Spo2TestStatus,
)
from app.schemas.providers.polar.sleepwise import (
    AlertnessJSON,
    CircadianBedtimeJSON,
    CircadianBedtimeQuality,
    CircadianBedtimeResultType,
    GradeType,
)
from app.schemas.sync_status import DataTypeFailure
from app.services.event_record_service import event_record_service
from app.services.health_score_service import health_score_service
from app.services.providers.api_client import make_authenticated_request
from app.services.providers.polar.coverage import ACTIVITY_SERIES
from app.services.providers.polar.pull_budget import (
    RATE_LIMITED,
    claim_continuous_hr_webhook,
    full_due,
    interrupted_full_fetches,
    record_full_end,
    record_full_failure,
    record_full_fetch,
    record_full_start,
)
from app.services.providers.templates.base_247_data import Base247DataTemplate
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.raw_payload_storage import store_raw_payload
from app.services.sync_error_code import (
    POLAR_FULL_FETCH_INTERRUPTED,
    POLAR_SLEEP_LISTING_MALFORMED,
    UNKNOWN,
    error_code,
)
from app.services.timeseries_service import timeseries_service
from app.utils.structured_logging import log_structured

_T = TypeVar("_T", bound=BaseModel)

_SLEEP_LISTING = "/v3/users/sleep/available"

# FORK (2.96.8, spec v1.1 P3): a date with no offset of its own takes the nearest one this many
# days before or after it; the earlier of two equally near.
_OFFSET_SEARCH_DAYS = 3
_ZONE_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


class _ContinuousHrOffsets:
    """FORK (2.96.8, spec v1.1 P1, P3): the UTC offset in force on each local date, read at ingest.

    Polar's continuous heart rate carries none. It comes from Polar's sleep listing, whose night
    start and end times carry the offset, and from the ``zone_offset`` stored on Polar workouts.
    Held for one ingest and stored nowhere (R-2026-10-21-c). ``uncorrected`` counts the samples
    that had no offset within ``_OFFSET_SEARCH_DAYS`` and were stored as before 2.96.8.
    """

    def __init__(self, by_date: dict[date, timedelta]) -> None:
        self._by_date = by_date
        self.uncorrected = 0

    def for_date(self, day: date) -> timedelta | None:
        for distance in range(_OFFSET_SEARCH_DAYS + 1):
            for candidate in (day - timedelta(days=distance), day + timedelta(days=distance)):
                if candidate in self._by_date:
                    return self._by_date[candidate]
        return None


# FORK (2.96.6.1, spec Q5): nothing whose date is more than this many days back is fetched.
POLAR_WINDOW_DAYS = 90

# FORK (2.96.6.1, spec causes 1-3 and 8, Andrei's ruling of 14 Oct 2026): a full fetch asks each
# range endpoint for the whole window Polar serves it, capped at POLAR_WINDOW_DAYS, instead of
# only what came after the connection's cursor. A night or reading that reaches Polar late, or
# whose earlier fetch failed, is therefore asked for again while Polar still serves it. How often
# a type is fetched in full is set by pull_budget (spec R1, see _FULL_FORM_TYPES). Unchanged rows
# are not re-emitted (emit_only_written). Per Polar AccessLink v3:
# - activities accept a ``from`` up to 365 days back in 28-day ranges: the 90-day cap applies;
# - SleepWise, body and skin temperature, SpO2 and wrist ECG serve "the last 28 days" with a
#   28-day maximum range; continuous heart rate and cardio load document no older history,
#   so they get the same 28 days. Widening one is a change to this table only.
# Sleep and Nightly Recharge are not here: both are listed by Polar for its last 28 days
# (``/sleep/available``, ``/nightly-recharge``) and filtered to POLAR_WINDOW_DAYS.
_RANGE_WINDOW_DAYS: dict[str, int] = {
    "daily_activity": POLAR_WINDOW_DAYS,
    "continuous_hr": 27,
    "cardio_load": 27,
    "alertness": 27,
    "circadian_bedtime": 27,
    "body_temperature": 27,
    "sleep_skin_temperature": 27,
    "spo2": 27,
    "wrist_ecg": 27,
}


# FORK (2.96.6.1, spec R1, AC2, AC17): the data types pull_budget fetches in full at most once per
# FULL_INTERVAL. Between full fetches a pull fetches the short form of sleep (the newest night
# Polar lists, when it is not stored) and of daily activity (yesterday and today), and nothing
# of the other eight. Nightly Recharge is one call for everything Polar serves, so it is fetched
# whole on every pull (spec cause 8, AC11), as are workouts (one call, see PolarWorkouts).
_FULL_FORM_TYPES: tuple[str, ...] = (
    "sleep",
    "daily_activity",
    *(t for t in _RANGE_WINDOW_DAYS if t != "daily_activity"),
)
# The key a short-form fetch is reported under, so a run says which form it fetched.
_SHORT_FORM_KEYS: dict[str, str] = {"sleep": "sleep_latest", "daily_activity": "daily_activity_recent"}
_SHORT_ACTIVITY_DAYS = 1

# FORK: a full fetch saves and commits its samples in batches of about this many, so a type's
# whole window is never held as sample objects at once. One full fetch is up to ~147k samples
# for a member (prod, 15 Oct 2026); built whole, every Polar member pulled together exceeded the
# worker's 1 GiB. A record whose samples alone exceed it (a day of heart rate) is one batch.
_SAVE_BATCH = 5000


class Polar247Data(Base247DataTemplate):
    def __init__(
        self,
        provider_name: str,
        api_base_url: str,
        oauth: BaseOAuthTemplate,
    ):
        super().__init__(provider_name, api_base_url, oauth)
        self.connection_repo = UserConnectionRepository()

    def _make_api_request(
        self,
        db: DbSession,
        user_id: UUID,
        endpoint: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            result = make_authenticated_request(
                db=db,
                user_id=user_id,
                connection_repo=self.connection_repo,
                oauth=self.oauth,
                api_base_url=self.api_base_url,
                provider_name=self.provider_name,
                endpoint=endpoint,
                method="GET",
                params=params,
                headers=headers,
                # FORK (2.96.6.1, spec R1): a 429 is not retried; the pull ends on it.
                retry_on_rate_limit=False,
            )
            store_raw_payload(
                source="api_response",
                provider="polar",
                payload=result,
                user_id=str(user_id),
                trace_id=endpoint,
            )
            return result
        except HTTPException as e:
            # 404 = no data for this date / feature not available on this device
            if e.status_code == status.HTTP_404_NOT_FOUND:
                return None
            # 204 No Content: api_client raises 500 wrapping a JSONDecodeError on empty body
            if e.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR and "Expecting value" in str(e.detail):
                return None
            raise

    def _parse_time_key(self, key: str) -> time:
        parts = key.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
        return time(h, m, s)

    def _hhmm_to_datetimes(
        self,
        items: dict[str, Any],
        anchor: datetime,
    ) -> list[tuple[datetime, Any]]:
        """Convert dict[HH:MM or HH:MM:SS, value] to [(datetime, value)], handling midnight crossover."""
        result: list[tuple[datetime, Any]] = []
        current_date = anchor.date()
        prev_t: time | None = None
        for key, val in items.items():
            t = self._parse_time_key(key)
            if prev_t is not None and t < prev_t:
                current_date += timedelta(days=1)
            result.append((datetime.combine(current_date, t, tzinfo=anchor.tzinfo), val))
            prev_t = t
        return result

    def _parse(self, raw: dict[str, Any], schema: type[_T], user_id: UUID, context: str) -> _T | None:
        try:
            return schema.model_validate(raw)
        except ValidationError as e:
            log_structured(
                self.logger, "warning", f"Polar {context} validation error: {e}", provider="polar", user_id=str(user_id)
            )
            return None

    # -------------------------------------------------------------------------
    # Sleep - GET /v3/users/sleep, GET /v3/users/sleep/{date} and GET /v3/users/sleep/available
    # -------------------------------------------------------------------------

    def _get_available_sleep_dates(self, db: DbSession, user_id: UUID) -> set[date]:
        response = self._make_api_request(db, user_id, _SLEEP_LISTING)
        nights = (response or {}).get("available", [])
        return {date.fromisoformat(night["date"]) for night in nights if night.get("date")}

    def get_sleep_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        date_range = {
            start_time.date() + timedelta(days=i) for i in range((end_time.date() - start_time.date()).days + 1)
        }
        available_dates = self._get_available_sleep_dates(db, user_id)
        sleep_data = []
        for d in date_range.intersection(available_dates):
            response = self._make_api_request(db, user_id, f"/v3/users/sleep/{d.isoformat()}")
            if response:
                sleep_data.append(response)
        return sleep_data

    def _parse_hypnogram(
        self,
        hypnogram: dict[str, int],
        sleep_start: datetime,
        sleep_end: datetime,
    ) -> list[SleepStage]:
        entries = self._hhmm_to_datetimes(hypnogram, sleep_start)
        if not entries:
            return []

        # Group consecutive runs of the same stage into a single SleepStage
        stages: list[SleepStage] = []
        group_start, current_val = entries[0]

        for dt, stage_val in entries[1:]:
            if stage_val != current_val:
                stage_type = HYPNOGRAM_STAGE_MAP.get(current_val)
                if stage_type is not None:
                    stages.append(SleepStage(stage=stage_type, start_time=group_start, end_time=dt))
                group_start = dt
                current_val = stage_val

        stage_type = HYPNOGRAM_STAGE_MAP.get(current_val)
        if stage_type is not None:
            stages.append(SleepStage(stage=stage_type, start_time=group_start, end_time=sleep_end))
        return stages

    def _parse_sleep_hr_samples(
        self,
        hr_samples: dict[str, int],
        sleep_start: datetime,
        user_id: UUID,
    ) -> list[TimeSeriesSampleCreate]:
        return [
            TimeSeriesSampleCreate(
                id=uuid4(),
                user_id=user_id,
                provider=ProviderName.POLAR,
                source=ProviderName.POLAR,
                recorded_at=dt,
                value=bpm,
                series_type=SeriesType.heart_rate,
            )
            for dt, bpm in self._hhmm_to_datetimes(hr_samples, sleep_start)
        ]

    SleepNormalized = tuple[
        EventRecordCreate, EventRecordDetailCreate, HealthScoreCreate | None, list[TimeSeriesSampleCreate]
    ]

    def normalize_sleep(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> list[SleepNormalized]:  # ty:ignore[invalid-method-override]
        results: list[Polar247Data.SleepNormalized] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, SleepJSON, user_id, "sleep")) is None:
                continue
            if not parsed.sleep_start_time or not parsed.sleep_end_time:
                log_structured(
                    self.logger,
                    "warning",
                    f"Polar sleep record missing start/end time: {parsed.date}",
                    provider="polar",
                    user_id=str(user_id),
                )
                continue

            sleep_id = uuid4()
            start_dt = datetime.fromisoformat(parsed.sleep_start_time)
            end_dt = datetime.fromisoformat(parsed.sleep_end_time)
            duration_seconds = int((end_dt - start_dt).total_seconds())

            light_s = parsed.light_sleep or 0
            deep_s = parsed.deep_sleep or 0
            rem_s = parsed.rem_sleep or 0
            sleep_stages = self._parse_hypnogram(parsed.hypnogram, start_dt, end_dt) if parsed.hypnogram else None

            record = EventRecordCreate(
                id=sleep_id,
                category="sleep",
                type="sleep_session",
                source_name="Polar",
                duration_seconds=duration_seconds,
                start_datetime=start_dt,
                end_datetime=end_dt,
                provider=ProviderName.POLAR,
                user_id=user_id,
            )
            detail = EventRecordDetailCreate(
                record_id=sleep_id,
                sleep_total_duration_minutes=(light_s + deep_s + rem_s) // 60,
                sleep_time_in_bed_minutes=duration_seconds // 60 if duration_seconds else None,
                sleep_deep_minutes=deep_s // 60,
                sleep_light_minutes=light_s // 60,
                sleep_rem_minutes=rem_s // 60,
                sleep_awake_minutes=(parsed.total_interruption_duration or 0) // 60,
                sleep_stages=sleep_stages,
            )

            score: HealthScoreCreate | None = None
            if parsed.sleep_score is not None:
                raw_components: dict[str, float | int | None] = {
                    "sleep_time": parsed.group_duration_score,
                    "long_interruptions": parsed.long_interruption_duration,
                    "continuity": parsed.continuity,
                    "actual_sleep": parsed.group_solidity_score,
                    "rem_sleep": parsed.rem_sleep,
                    "deep_sleep": parsed.deep_sleep,
                }
                components: dict[str, ScoreComponent] = {
                    k: ScoreComponent(value=v) for k, v in raw_components.items() if v is not None
                }
                score = HealthScoreCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.POLAR,
                    category=HealthScoreCategory.SLEEP,
                    value=parsed.sleep_score,
                    recorded_at=start_dt,
                    components=components or None,
                    event_record_id=sleep_id,
                )

            hr_samples = (
                self._parse_sleep_hr_samples(parsed.heart_rate_samples, start_dt, user_id)
                if parsed.heart_rate_samples
                else []
            )
            results.append((record, detail, score, hr_samples))
        return results

    # -------------------------------------------------------------------------
    # Daily Activity - GET /v3/users/activities
    # -------------------------------------------------------------------------

    def get_daily_activity_statistics(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[dict[str, Any]]:
        # Polar enforces a max 28-day window per request
        results: list[dict[str, Any]] = []
        chunk_start = start_date
        while chunk_start < end_date:
            chunk_end = min(chunk_start + timedelta(days=27), end_date)
            params = {
                "from": chunk_start.date().isoformat(),
                "to": chunk_end.date().isoformat(),
                "steps": "true",
                "activity_zones": "false",
                "inactivity_stamps": "false",
            }
            response = self._make_api_request(db, user_id, "/v3/users/activities", params=params)
            results.extend(response or [])
            chunk_start = chunk_end + timedelta(days=1)
        return results

    def normalize_daily_activity(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> list[TimeSeriesSampleCreate]:  # ty:ignore[invalid-method-override]
        samples: list[TimeSeriesSampleCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, DailyActivityJSON, user_id, "daily_activity")) is None:
                continue
            if not parsed.start_time:
                continue
            recorded_at = datetime.fromisoformat(parsed.start_time)
            for attr, series_type in ACTIVITY_SERIES.items():
                value = getattr(parsed, attr)
                if value is None:
                    continue
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=recorded_at,
                        value=Decimal(str(value)),
                        series_type=series_type,
                        is_daily_total=True,
                    )
                )
        return samples

    # -------------------------------------------------------------------------
    # Continuous Heart Rate - GET /v3/users/continuous-heart-rate/{date}
    # -------------------------------------------------------------------------

    def get_continuous_hr_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        chunk_start = start_time
        while chunk_start < end_time:
            chunk_end = min(chunk_start + timedelta(days=27), end_time)
            params = {"from": chunk_start.date().isoformat(), "to": chunk_end.date().isoformat()}
            response = self._make_api_request(db, user_id, "/v3/users/continuous-heart-rate", params=params)
            results.extend((response or {}).get("heart_rates", []))
            chunk_start = chunk_end + timedelta(days=1)
        return results

    def normalize_continuous_hr(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
        offsets: _ContinuousHrOffsets,
    ) -> list[TimeSeriesSampleCreate]:
        """FORK (2.96.8, spec v1.1 P1, AC1): Polar sends a bare local date and local ``HH:MM`` times,
        so the true instant is local minus the offset in force that day (``offsets``). These used
        to be stored as if they were UTC: one offset late. A date Polar sends with a zone is
        converted as given. A date with no offset is stored as before and counted (P3)."""
        samples: list[TimeSeriesSampleCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, ContinuousHeartRateJSON, user_id, "continuous_hr")) is None:
                continue
            if not parsed.date or not parsed.heart_rate_samples:
                continue
            anchor = datetime.fromisoformat(parsed.date)
            samples_dict = {s.sample_time: s.heart_rate for s in parsed.heart_rate_samples if s.sample_time}
            if anchor.tzinfo is None:
                offset = offsets.for_date(anchor.date())
                if offset is None:
                    offsets.uncorrected += len(samples_dict)
                anchor = anchor.replace(tzinfo=timezone(offset) if offset is not None else timezone.utc)
            samples.extend(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.POLAR,
                    source=ProviderName.POLAR,
                    recorded_at=dt.astimezone(timezone.utc),
                    value=bpm,
                    series_type=SeriesType.heart_rate,
                )
                for dt, bpm in self._hhmm_to_datetimes(samples_dict, anchor)
            )
        return samples

    def _continuous_hr_offsets(
        self, db: DbSession, user_id: UUID, listing: Any, raw_items: list[dict[str, Any]]
    ) -> _ContinuousHrOffsets:
        """The offsets ``normalize_continuous_hr`` reads (P1): the sleep listing first, a night's end
        setting its date over another night's start, then the member's stored Polar workouts."""
        by_date: dict[date, timedelta] = {}
        entries = [e for e in (listing or {}).get("available", []) if isinstance(e, dict)]
        for key in ("start_time", "end_time"):
            for entry in entries:
                try:
                    moment = datetime.fromisoformat(entry[key])
                except (KeyError, TypeError, ValueError):
                    continue
                if (offset := moment.utcoffset()) is not None:
                    by_date[moment.date()] = offset
        days = []
        for raw in raw_items:
            try:
                days.append(date.fromisoformat(str(raw.get("date"))[:10]))
            except (AttributeError, ValueError):
                continue
        if days:
            # The bounds are UTC midnights and a workout's local day is not its UTC day: one day of
            # margin past the search covers a local day anywhere from -12:00 to +14:00 (Codex, #45).
            margin = timedelta(days=_OFFSET_SEARCH_DAYS + 2)
            workouts = (
                db.query(EventRecord.start_datetime, EventRecord.zone_offset)
                .join(DataSource, EventRecord.data_source_id == DataSource.id)
                .filter(
                    DataSource.user_id == user_id,
                    DataSource.provider == ProviderName.POLAR,
                    EventRecord.category == "workout",
                    EventRecord.zone_offset.isnot(None),
                    EventRecord.start_datetime >= datetime.combine(min(days) - margin, time(), tzinfo=timezone.utc),
                    EventRecord.start_datetime < datetime.combine(max(days) + margin, time(), tzinfo=timezone.utc),
                )
                .order_by(EventRecord.start_datetime)
                .all()
            )
            for start, zone_offset in workouts:
                if (match := _ZONE_OFFSET.match(zone_offset or "")) is None:
                    continue
                sign = -1 if match.group(1) == "-" else 1
                offset = sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
                by_date.setdefault(start.astimezone(timezone(offset)).date(), offset)
        return _ContinuousHrOffsets(by_date)

    def _report_uncorrected(self, offsets: _ContinuousHrOffsets, user_id: UUID) -> None:
        """P3: the samples stored without an offset, as a count the build reports from."""
        if offsets.uncorrected:
            log_structured(
                self.logger,
                "info",
                "Polar continuous heart rate stored without an offset",
                provider="polar",
                action="polar_continuous_hr_uncorrected",
                user_id=str(user_id),
                count=offsets.uncorrected,
            )

    def _save_continuous_hr(self, db: DbSession, user_id: UUID, listing: Any, raw_items: list[dict[str, Any]]) -> int:
        offsets = self._continuous_hr_offsets(db, user_id, listing, raw_items)
        saved = self._save_timeseries_in_batches(
            db, raw_items, lambda items, uid: self.normalize_continuous_hr(items, uid, offsets), user_id
        )
        self._report_uncorrected(offsets, user_id)
        return saved

    def _save_continuous_hr_from_webhook(self, db: DbSession, user_id: UUID, path: str) -> dict[str, int]:
        """FORK (2.96.8, P1, P7): the same offsets as the pull, so the webhook and the full fetch
        store a sample at one instant (AC7). That costs one more call, the sleep listing, and the
        webhooks that call Polar are capped a day. A refused one calls nothing and stores nothing:
        the day is the next full fetch's. A listing that fails stores nothing either."""
        if (refused := claim_continuous_hr_webhook(user_id, datetime.now(timezone.utc))) is not None:
            log_structured(
                self.logger,
                "info",
                "Polar continuous heart-rate webhook left to the next full fetch",
                provider="polar",
                action="polar_continuous_hr_webhook_deferred",
                user_id=str(user_id),
                reason=refused,
            )
            return {"continuous_hr": 0}
        raw = self._make_api_request(db, user_id, path)
        if not raw:
            return {}
        items = raw.get("heart_rates", [])
        offsets = self._continuous_hr_offsets(db, user_id, self._make_api_request(db, user_id, _SLEEP_LISTING), items)
        saved = self._save_timeseries(db, self.normalize_continuous_hr(items, user_id, offsets))
        self._report_uncorrected(offsets, user_id)
        return {"continuous_hr": saved}

    # -------------------------------------------------------------------------
    # Cardio Load - GET /v3/users/cardio-load/{date}
    # -------------------------------------------------------------------------

    def get_cardio_load_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        chunk_start = start_time
        while chunk_start < end_time:
            chunk_end = min(chunk_start + timedelta(days=27), end_time)
            params = {"from": chunk_start.date().isoformat(), "to": chunk_end.date().isoformat()}
            response = self._make_api_request(db, user_id, "/v3/users/cardio-load/date", params=params)
            results.extend(response or [])
            chunk_start = chunk_end + timedelta(days=1)
        return results

    def normalize_cardio_load(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> list[HealthScoreCreate]:
        scores: list[HealthScoreCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, CardioLoadJSON, user_id, "cardio_load")) is None:
                continue
            if parsed.cardio_load is None or not parsed.date:
                continue
            if parsed.cardio_load_status == "LOAD_STATUS_NOT_AVAILABLE":
                continue
            raw_components: dict[str, float | int | None] = {
                "strain": parsed.strain,
                "tolerance": parsed.tolerance,
                "cardio_load_ratio": parsed.cardio_load_ratio,
            }
            if parsed.cardio_load_level:
                lvl = parsed.cardio_load_level
                raw_components.update(
                    {
                        "level_very_low": lvl.very_low,
                        "level_low": lvl.low,
                        "level_medium": lvl.medium,
                        "level_high": lvl.high,
                        "level_very_high": lvl.very_high,
                    }
                )
            components = {k: ScoreComponent(value=v) for k, v in raw_components.items() if v is not None}
            scores.append(
                HealthScoreCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.POLAR,
                    category=HealthScoreCategory.STRAIN,
                    value=parsed.cardio_load,
                    recorded_at=datetime.fromisoformat(parsed.date),
                    components=components or None,
                )
            )
        return scores

    # -------------------------------------------------------------------------
    # Nightly Recharge - GET /v3/users/nightly-recharge/{date}
    # -------------------------------------------------------------------------

    def get_nightly_recharge_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        # FORK (2.96.6.1, spec cause 8, AC11): Polar sends Nightly Recharge by pull only, and this
        # list (Polar's last 28 days) is requested on every pull, so a date still missing it is
        # asked for again each time. Dates older than POLAR_WINDOW_DAYS are dropped (Q5).
        response = self._make_api_request(db, user_id, "/v3/users/nightly-recharge")
        oldest = (datetime.now(timezone.utc) - timedelta(days=POLAR_WINDOW_DAYS)).date().isoformat()
        return [r for r in (response or {}).get("recharges", []) if (r.get("date") or "") >= oldest]

    def normalize_nightly_recharge(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> tuple[list[HealthScoreCreate], list[TimeSeriesSampleCreate]]:
        scores: list[HealthScoreCreate] = []
        samples: list[TimeSeriesSampleCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, NightlyRechargeJSON, user_id, "nightly_recharge")) is None:
                continue
            if parsed.nightly_recharge_status is None or not parsed.date:
                continue
            components: dict[str, ScoreComponent] = {}
            for key, val in {
                "heart_rate_avg": parsed.heart_rate_avg,
                "beat_to_beat_avg": parsed.beat_to_beat_avg,
                "heart_rate_variability_avg": parsed.heart_rate_variability_avg,
                "breathing_rate_avg": parsed.breathing_rate_avg,
                "ans_charge": parsed.ans_charge,
            }.items():
                if val is not None:
                    components[key] = ScoreComponent(value=val)
            if parsed.ans_charge_status is not None:
                components["ans_charge_status"] = ScoreComponent(
                    value=parsed.ans_charge_status,
                    qualifier=ANS_CHARGE_STATUS_LABELS.get(parsed.ans_charge_status),
                )
            scores.append(
                HealthScoreCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.POLAR,
                    category=HealthScoreCategory.RECOVERY,
                    value=parsed.nightly_recharge_status,
                    qualifier=NIGHTLY_RECHARGE_STATUS_LABELS.get(parsed.nightly_recharge_status),
                    recorded_at=datetime.fromisoformat(parsed.date),
                    components=components or None,
                )
            )
            # Bridge: also emit the Recharge HRV average as an RMSSD timeseries
            # sample. The health-score component above is off the timeseries read
            # path, so downstream consumers that read the
            # heart_rate_variability_rmssd series (e.g. readiness) would otherwise
            # never see it. heart_rate_variability_avg is RMSSD in ms (see
            # NightlyRechargeJSON) — the same metric and unit WHOOP emits.
            if parsed.heart_rate_variability_avg is not None:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=datetime.fromisoformat(parsed.date).replace(tzinfo=timezone.utc),
                        value=parsed.heart_rate_variability_avg,
                        series_type=SeriesType.heart_rate_variability_rmssd,
                    )
                )
            # Bridge: likewise emit the Recharge average heart rate as a
            # resting_heart_rate sample (same rationale as the HRV bridge — it is
            # only a health-score component otherwise, off the timeseries read
            # path). heart_rate_avg is the average HR over the recharge window
            # (~first 4h of sleep) in bpm — a nightly resting HR used downstream
            # within-person for co-suppression, not an absolute clinical resting
            # HR. The worker already reads resting_heart_rate → physiology_daily
            # .rhr_bpm, so no downstream change is required.
            if parsed.heart_rate_avg is not None:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=datetime.fromisoformat(parsed.date).replace(tzinfo=timezone.utc),
                        value=parsed.heart_rate_avg,
                        series_type=SeriesType.resting_heart_rate,
                    )
                )
        return scores, samples

    # -------------------------------------------------------------------------
    # SleepWise — Alertness: GET /v3/users/sleepwise/alertness
    # -------------------------------------------------------------------------

    def get_alertness_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        params = {
            "from": start_time.date().isoformat(),
            "to": end_time.date().isoformat(),
        }
        response = self._make_api_request(db, user_id, "/v3/users/sleepwise/alertness", params=params)
        return response or []

    def normalize_alertness(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> list[HealthScoreCreate]:
        scores: list[HealthScoreCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, AlertnessJSON, user_id, "alertness")) is None:
                continue
            # Only score primary results — additional are supplementary assessments
            if parsed.grade is None or parsed.grade_type != GradeType.PRIMARY:
                continue
            if not parsed.period_start_time:
                continue
            components: dict[str, ScoreComponent] = {}
            if parsed.grade_validity_seconds is not None:
                components["grade_validity_seconds"] = ScoreComponent(value=parsed.grade_validity_seconds)
            if parsed.sleep_inertia is not None:
                components["sleep_inertia"] = ScoreComponent(
                    value=None,
                    qualifier=SLEEP_INERTIA_LABELS.get(parsed.sleep_inertia),
                )
            if parsed.sleep_type is not None:
                components["sleep_type"] = ScoreComponent(value=None, qualifier=parsed.sleep_type.value)
            scores.append(
                HealthScoreCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.POLAR,
                    category=HealthScoreCategory.READINESS,
                    value=parsed.grade,
                    qualifier=GRADE_CLASSIFICATION_LABELS.get(parsed.grade_classification)
                    if parsed.grade_classification
                    else None,
                    recorded_at=datetime.fromisoformat(parsed.period_start_time),
                    components=components or None,
                )
            )
        return scores

    # -------------------------------------------------------------------------
    # SleepWise — Circadian Bedtime: GET /v3/users/sleepwise/circadian-bedtime
    # -------------------------------------------------------------------------

    def get_circadian_bedtime_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        params = {
            "from": start_time.date().isoformat(),
            "to": end_time.date().isoformat(),
        }
        response = self._make_api_request(db, user_id, "/v3/users/sleepwise/circadian-bedtime", params=params)
        return response or []

    def normalize_circadian_bedtime(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> list[HealthScoreCreate]:
        scores: list[HealthScoreCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, CircadianBedtimeJSON, user_id, "circadian_bedtime")) is None:
                continue
            if parsed.quality is None or parsed.quality == CircadianBedtimeQuality.UNKNOWN:
                continue
            if not parsed.period_start_time:
                continue
            components: dict[str, ScoreComponent] = {}
            if parsed.sleep_gate_start_time and parsed.sleep_gate_end_time:
                components["sleep_gate_start"] = ScoreComponent(value=None, qualifier=parsed.sleep_gate_start_time)
                components["sleep_gate_end"] = ScoreComponent(value=None, qualifier=parsed.sleep_gate_end_time)
            if parsed.result_type is not None and parsed.result_type != CircadianBedtimeResultType.UNKNOWN:
                components["result_type"] = ScoreComponent(value=None, qualifier=parsed.result_type.value)
            scores.append(
                HealthScoreCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.POLAR,
                    category=HealthScoreCategory.SLEEP,
                    value=CIRCADIAN_QUALITY_VALUES.get(parsed.quality),
                    qualifier=CIRCADIAN_QUALITY_LABELS.get(parsed.quality),
                    recorded_at=datetime.fromisoformat(parsed.period_start_time),
                    components=components or None,
                )
            )
        return scores

    # -------------------------------------------------------------------------
    # Elixir — Body Temperature: GET /v3/users/body-temperature
    # -------------------------------------------------------------------------

    def get_body_temperature_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        params = {"from": start_time.date().isoformat(), "to": end_time.date().isoformat()}
        response = self._make_api_request(db, user_id, "/v3/users/body-temperature", params=params)
        return response or []

    def normalize_body_temperature(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> list[TimeSeriesSampleCreate]:
        samples: list[TimeSeriesSampleCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, BodyTemperaturePeriodJSON, user_id, "body_temperature")) is None:
                continue
            if not parsed.samples or not parsed.start_time or not parsed.measurement_type:
                continue
            series_type = BODY_TEMP_SERIES_TYPE.get(parsed.measurement_type)
            if series_type is None:
                continue
            anchor = datetime.fromisoformat(parsed.start_time)
            samples.extend(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.POLAR,
                    source=ProviderName.POLAR,
                    recorded_at=anchor + timedelta(milliseconds=s.recording_time_delta_milliseconds or 0),
                    value=s.temperature_celsius,
                    series_type=series_type,
                )
                for s in parsed.samples
                if s.temperature_celsius is not None
            )
        return samples

    # -------------------------------------------------------------------------
    # Elixir — Sleep Skin Temperature: GET /v3/users/sleep-skin-temperature
    # -------------------------------------------------------------------------

    def get_sleep_skin_temperature_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        params = {"from": start_time.date().isoformat(), "to": end_time.date().isoformat()}
        response = self._make_api_request(db, user_id, "/v3/users/sleep-skin-temperature", params=params)
        return response or []

    def normalize_sleep_skin_temperature(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> list[TimeSeriesSampleCreate]:
        samples: list[TimeSeriesSampleCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, SkinTemperatureJSON, user_id, "sleep_skin_temperature")) is None:
                continue
            if not parsed.sleep_date:
                continue
            recorded_at = datetime.fromisoformat(parsed.sleep_date)
            if parsed.sleep_time_skin_temperature_celsius is not None:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=recorded_at,
                        value=parsed.sleep_time_skin_temperature_celsius,
                        series_type=SeriesType.skin_temperature,
                    )
                )
            if parsed.deviation_from_baseline_celsius is not None:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=recorded_at,
                        value=parsed.deviation_from_baseline_celsius,
                        series_type=SeriesType.skin_temperature_deviation,
                    )
                )
        return samples

    # -------------------------------------------------------------------------
    # Elixir — SpO2: GET /v3/users/spo2
    # -------------------------------------------------------------------------

    def get_spo2_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        params = {"from": start_time.date().isoformat(), "to": end_time.date().isoformat()}
        response = self._make_api_request(db, user_id, "/v3/users/spo2", params=params)
        return response or []

    def normalize_spo2(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> list[TimeSeriesSampleCreate]:
        samples: list[TimeSeriesSampleCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, Spo2TestResultJSON, user_id, "spo2")) is None:
                continue
            if parsed.test_status != Spo2TestStatus.PASSED or parsed.test_time is None:
                continue
            recorded_at = datetime.fromtimestamp(parsed.test_time, tz=timezone.utc)
            if parsed.blood_oxygen_percent is not None:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=recorded_at,
                        value=parsed.blood_oxygen_percent,
                        series_type=SeriesType.oxygen_saturation,
                    )
                )
            if parsed.heart_rate_variability_ms is not None:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=recorded_at,
                        value=parsed.heart_rate_variability_ms,
                        series_type=SeriesType.heart_rate_variability_rmssd,
                    )
                )
        return samples

    # -------------------------------------------------------------------------
    # Elixir — Wrist ECG: GET /v3/users/wrist-ecg
    # -------------------------------------------------------------------------

    def get_wrist_ecg_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        params = {"from": start_time.date().isoformat(), "to": end_time.date().isoformat()}
        response = self._make_api_request(db, user_id, "/v3/users/wrist-ecg", params=params)
        return response or []

    def normalize_wrist_ecg(
        self,
        raw_items: list[dict[str, Any]],
        user_id: UUID,
    ) -> list[TimeSeriesSampleCreate]:
        samples: list[TimeSeriesSampleCreate] = []
        for raw in raw_items:
            if (parsed := self._parse(raw, EcgTestResultJSON, user_id, "wrist_ecg")) is None:
                continue
            if parsed.test_time is None:
                continue
            recorded_at = datetime.fromtimestamp(parsed.test_time, tz=timezone.utc)
            if parsed.heart_rate_variability_ms is not None:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=recorded_at,
                        value=parsed.heart_rate_variability_ms,
                        series_type=SeriesType.heart_rate_variability_rmssd,
                    )
                )
            if parsed.average_heart_rate_bpm is not None:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        provider=ProviderName.POLAR,
                        source=ProviderName.POLAR,
                        recorded_at=recorded_at,
                        value=parsed.average_heart_rate_bpm,
                        series_type=SeriesType.heart_rate,
                    )
                )
        return samples

    # -------------------------------------------------------------------------
    # Persistence helpers
    # -------------------------------------------------------------------------

    def _save_timeseries(
        self, db: DbSession, samples: list[TimeSeriesSampleCreate], *, emit_inline: bool = False
    ) -> int:
        counts: int = 0
        if samples:
            # FORK (2.96.6.1): every pull re-reads Polar's whole served window, so only the
            # samples the save actually wrote go out as webhooks.
            counts = timeseries_service.bulk_create_samples(
                db, samples, emit_only_written=True, emit_inline=emit_inline
            )
        return counts

    def _save_timeseries_in_batches(
        self,
        db: DbSession,
        raw_items: list[dict[str, Any]],
        normalize: Callable[[list[dict[str, Any]], UUID], list[TimeSeriesSampleCreate]],
        user_id: UUID,
    ) -> int:
        """Normalize ``raw_items`` one record at a time and save and commit every ``_SAVE_BATCH``
        samples. Consumes ``raw_items``, so a record already saved is released too. A batch that
        fails leaves the batches before it stored: the type is reported failed and fetched again."""
        counts: int = 0
        pending: list[TimeSeriesSampleCreate] = []
        raw_items.reverse()
        while raw_items:
            pending.extend(normalize([raw_items.pop()], user_id))
            if len(pending) >= _SAVE_BATCH:
                # emit_inline: the batch's webhooks are queued before the next batch is built.
                counts += self._save_timeseries(db, pending, emit_inline=True)
                db.commit()
                pending = []
        if pending:
            counts += self._save_timeseries(db, pending, emit_inline=True)
            db.commit()
        return counts

    def _save_scores(self, db: DbSession, scores: list[HealthScoreCreate]) -> int:
        if scores:
            health_score_service.bulk_create(db, scores)
        return len(scores)

    def _save_nightly_recharge(
        self,
        db: DbSession,
        scores: list[HealthScoreCreate],
        samples: list[TimeSeriesSampleCreate],
    ) -> int:
        """Persist Recharge health scores plus the bridged RMSSD HRV samples."""
        self._save_scores(db, scores)
        self._save_timeseries(db, samples)
        return len(scores)

    def fetch_and_save_from_webhook(
        self,
        db: DbSession,
        user_id: UUID,
        event_type: PolarWebhookEventType | str,
        path: str,
    ) -> dict[str, int]:
        """Fetch a single entity from the webhook URL path and save it. Used by webhook handler."""
        if event_type == PolarWebhookEventType.CONTINUOUS_HEART_RATE:
            return self._save_continuous_hr_from_webhook(db, user_id, path)
        raw = self._make_api_request(db, user_id, path)
        if not raw:
            return {}

        match event_type:
            case PolarWebhookEventType.SLEEP:
                # FORK (2.96.6.1): the same save as the pull, so the night is stored last.
                return {"sleep": self._save_sleep_items(db, user_id, [raw])}

            case PolarWebhookEventType.ACTIVITY_SUMMARY:
                return {"daily_activity": self._save_timeseries(db, self.normalize_daily_activity([raw], user_id))}

            case PolarWebhookEventType.SLEEP_WISE_ALERTNESS:
                return {"alertness": self._save_scores(db, self.normalize_alertness([raw], user_id))}

            case PolarWebhookEventType.SLEEP_WISE_CIRCADIAN_BEDTIME:
                return {"circadian_bedtime": self._save_scores(db, self.normalize_circadian_bedtime([raw], user_id))}

            case _:
                log_structured(
                    self.logger,
                    "warning",
                    f"Targeted webhook fetch not implemented for event type: {event_type}",
                    provider="polar",
                )
                return {}

    def _save_missing_sleep(
        self,
        db: DbSession,
        user_id: UUID,
        now: datetime,
        *,
        latest_only: bool = False,
        listing: Callable[[], Any] | None = None,
    ) -> int | DataTypeFailure:
        """Fetch and store every night Polar lists that is inside the window and not yet stored.

        FORK (2.96.6.1, spec causes 1-3, AC1-AC4). Polar lists the nights it holds for its last
        28 days, with each night's start and end. A night counts as stored when a Polar sleep
        record of this user overlaps that span; every other listed night whose date is at most
        POLAR_WINDOW_DAYS back is fetched, on every pull, whatever time it is and wherever the
        cursor stands. A night that reached Polar late, or whose fetch failed last time, is
        therefore fetched on the next pull. Each night is fetched and saved on its own, so one
        failing night does not discard the others; the type is then reported failed (with the
        first failure's code) and the failed night stays missing, so it is asked for again. A 429
        ends the loop: every later night would be refused too (spec R1).

        ``latest_only`` is the short form (spec R1): only the newest night Polar lists is
        considered, which is last night once the watch has synced.

        A listed entry whose date, start or end cannot be read is skipped on its own, so it
        cannot stop the valid nights being fetched (the newest valid one, in the short form);
        the type is then reported failed with ``POLAR_SLEEP_LISTING_MALFORMED``, unless a night's
        own failure gives an earlier code (a 429 above all, which ends the pull).

        ``listing`` (2.96.8): where the pull's one read of the listing comes from, so continuous
        heart rate reads its offsets from the same answer without another call.
        """
        oldest = (now - timedelta(days=POLAR_WINDOW_DAYS)).date()
        response = listing() if listing is not None else self._make_api_request(db, user_id, _SLEEP_LISTING)
        listed, malformed = self._readable_available_nights(response, user_id)
        if latest_only and listed:
            listed = [max(listed, key=lambda listed_night: listed_night[0])]
        missing = [night for day, night in listed if day >= oldest and not self._polar_sleep_stored(db, user_id, night)]

        saved = 0
        first_failure: str | None = None
        for night in missing:
            try:
                raw = self._make_api_request(db, user_id, f"/v3/users/sleep/{night['date']}")
                if raw:
                    saved += self._save_sleep_items(db, user_id, [raw])
                db.commit()
            except Exception as e:
                db.rollback()
                code = error_code(e) or UNKNOWN
                first_failure = first_failure or code
                log_structured(
                    self.logger,
                    "warning",
                    "Failed to sync a Polar night",
                    provider="polar",
                    task="_save_missing_sleep",
                    user_id=str(user_id),
                    error_code=code,
                )
                if code == RATE_LIMITED:
                    # Codex 4271871222: the 429 is what the night type reports, whatever failed
                    # before it, so the caller ends the whole pull on it (spec R1).
                    first_failure = RATE_LIMITED
                    break
        if first_failure is None and malformed:
            first_failure = POLAR_SLEEP_LISTING_MALFORMED
        if first_failure is not None:
            return DataTypeFailure(code=first_failure, saved=saved)
        return saved

    def _readable_available_nights(self, response: Any, user_id: UUID) -> tuple[list[tuple[date, dict[str, Any]]], int]:
        """The /sleep/available entries whose date, start and end can be read, and how many could not.

        An entry with no date is left out as before (there is no night to ask for); a start or
        end that is absent is allowed, as before. A value that is present and unreadable, or an
        entry that is not an object, makes the entry malformed. Logged as a code with a count.
        """
        readable: list[tuple[date, dict[str, Any]]] = []
        malformed = 0
        for entry in (response or {}).get("available", []):
            try:
                if not entry.get("date"):
                    continue
                day = date.fromisoformat(entry["date"])
                for key in ("start_time", "end_time"):
                    if entry.get(key):
                        datetime.fromisoformat(entry[key])
            except (AttributeError, TypeError, ValueError):
                malformed += 1
                continue
            readable.append((day, entry))
        if malformed:
            log_structured(
                self.logger,
                "warning",
                "Skipped unreadable Polar sleep listing entries",
                provider="polar",
                task="_save_missing_sleep",
                user_id=str(user_id),
                error_code=POLAR_SLEEP_LISTING_MALFORMED,
                count=malformed,
            )
        return readable, malformed

    @staticmethod
    def _polar_sleep_stored(db: DbSession, user_id: UUID, night: dict[str, Any]) -> bool:
        """True when a Polar sleep record of this user overlaps the night Polar listed."""
        start, end = night.get("start_time"), night.get("end_time")
        if not start or not end:
            return False
        return (
            db.query(EventRecord.id)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(
                DataSource.user_id == user_id,
                DataSource.provider == ProviderName.POLAR,
                EventRecord.category == "sleep",
                EventRecord.start_datetime < datetime.fromisoformat(end),
                EventRecord.end_datetime > datetime.fromisoformat(start),
            )
            .first()
            is not None
        )

    def _save_sleep_items(self, db: DbSession, user_id: UUID, raw_items: list[dict[str, Any]]) -> int:
        """Store sleep records with their scores and heart-rate samples. Raises on any failure."""
        count = 0
        for record, detail, score, hr in self.normalize_sleep(raw_items, user_id):
            # Flush the sleep interval before its HR so the frozen first night's evening
            # has association proof. Keep HR and score in the existing before-commit hook:
            # after that single commit the night counts as stored and is not fetched again.
            # A failed contributor still rolls back the night and leaves it retryable.
            stored = event_record_service.create_or_merge_sleep(
                db,
                user_id,
                record,
                detail,
                settings.sleep_end_gap_minutes,
                before_commit=self._sleep_contributors_writer(db, hr, score),
            )
            count += int(stored is not None)
        return count

    def _sleep_contributors_writer(
        self, db: DbSession, samples: list[TimeSeriesSampleCreate], score: HealthScoreCreate | None
    ) -> Callable[[EventRecord], None]:
        """The night's HR and score, written in its own transaction after the interval exists.

        The score cannot be written before the record: it references the record (foreign key).
        It points at the record the night ended up in, which after a merge is not the id
        ``normalize_sleep`` minted.
        """

        def write(stored: EventRecord) -> None:
            self._save_timeseries(db, samples)
            if score is not None:
                self._save_scores(db, [score.model_copy(update={"event_record_id": stored.id})])

        return write

    # -------------------------------------------------------------------------
    # Load and save all — entry point for sync_vendor_data task
    # -------------------------------------------------------------------------

    def load_and_save_all(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
        is_first_sync: bool = False,
    ) -> dict[str, int | DataTypeFailure]:
        # FORK (2.96.6.1): the window no longer follows ``start_time`` / ``end_time`` (the
        # connection's cursor on a live pull). A type fetched in full asks for what Polar serves
        # it, capped at POLAR_WINDOW_DAYS (see _RANGE_WINDOW_DAYS); which types are fetched in
        # full, and the short forms, are under _FULL_FORM_TYPES. The parameters stay for the
        # shared provider interface.
        now = datetime.now(timezone.utc)

        def since(data_type: str) -> datetime:
            return now - timedelta(days=_RANGE_WINDOW_DAYS[data_type])

        # FORK (2.96.6.1, spec R1): which types this pull fetches in full. Without a connection
        # (nothing to keep the state on) every type is fetched in full, as before the budget.
        connection = self.connection_repo.get_by_user_and_provider(db, user_id, self.provider_name)
        connection_id = connection.id if connection is not None else None
        due = (
            full_due(db, connection_id, now, _FULL_FORM_TYPES)
            if connection_id is not None
            else frozenset(_FULL_FORM_TYPES)
        )

        # FORK (2.96.8, P7): the sleep listing is read once a pull, by the sleep task, which runs
        # first; continuous heart rate reads its offsets from that answer. A listing that failed
        # fails continuous heart rate too, without asking again.
        listed: list[Any] = []

        def sleep_listing() -> Any:
            if not listed:
                try:
                    listed.append(self._make_api_request(db, user_id, _SLEEP_LISTING))
                except Exception as e:
                    listed.append(e)
            if isinstance(listed[0], Exception):
                raise listed[0]
            return listed[0]

        def daily_activity(start: datetime) -> Callable[[], Any]:
            return lambda: self._save_timeseries_in_batches(
                db, self.get_daily_activity_statistics(db, user_id, start, now), self.normalize_daily_activity, user_id
            )

        full_only: dict[str, Callable[[], Any]] = {
            "continuous_hr": lambda: self._save_continuous_hr(
                db,
                user_id,
                sleep_listing(),
                self.get_continuous_hr_data(db, user_id, since("continuous_hr"), now),
            ),
            "cardio_load": lambda: self._save_scores(
                db,
                self.normalize_cardio_load(self.get_cardio_load_data(db, user_id, since("cardio_load"), now), user_id),
            ),
            "alertness": lambda: self._save_scores(
                db, self.normalize_alertness(self.get_alertness_data(db, user_id, since("alertness"), now), user_id)
            ),
            "circadian_bedtime": lambda: self._save_scores(
                db,
                self.normalize_circadian_bedtime(
                    self.get_circadian_bedtime_data(db, user_id, since("circadian_bedtime"), now), user_id
                ),
            ),
            "body_temperature": lambda: self._save_timeseries_in_batches(
                db,
                self.get_body_temperature_data(db, user_id, since("body_temperature"), now),
                self.normalize_body_temperature,
                user_id,
            ),
            "sleep_skin_temperature": lambda: self._save_timeseries_in_batches(
                db,
                self.get_sleep_skin_temperature_data(db, user_id, since("sleep_skin_temperature"), now),
                self.normalize_sleep_skin_temperature,
                user_id,
            ),
            "spo2": lambda: self._save_timeseries_in_batches(
                db, self.get_spo2_data(db, user_id, since("spo2"), now), self.normalize_spo2, user_id
            ),
            "wrist_ecg": lambda: self._save_timeseries_in_batches(
                db, self.get_wrist_ecg_data(db, user_id, since("wrist_ecg"), now), self.normalize_wrist_ecg, user_id
            ),
        }

        # Sleep first: last night is what the morning needs, and a 429 ends the pull.
        tasks: dict[str, Callable[[], Any]] = {}
        if "sleep" in due:
            tasks["sleep"] = lambda: self._save_missing_sleep(db, user_id, now, listing=sleep_listing)
        else:
            tasks[_SHORT_FORM_KEYS["sleep"]] = lambda: self._save_missing_sleep(
                db, user_id, now, latest_only=True, listing=sleep_listing
            )
        tasks["nightly_recharge"] = lambda: self._save_nightly_recharge(
            db,
            *self.normalize_nightly_recharge(self.get_nightly_recharge_data(db, user_id, now, now), user_id),
        )
        if "daily_activity" in due:
            tasks["daily_activity"] = daily_activity(since("daily_activity"))
        else:
            tasks[_SHORT_FORM_KEYS["daily_activity"]] = daily_activity(now - timedelta(days=_SHORT_ACTIVITY_DAYS))
        tasks.update({data_type: fn for data_type, fn in full_only.items() if data_type in due})

        results: dict[str, int | DataTypeFailure] = {}
        # FORK: a full fetch an earlier pull started and never ended (record_full_start). Said once:
        # the marker is dropped here. A type its gap still holds back reads FAILED in this run; one
        # due again runs below and reports what that fetch does.
        if connection_id is not None:
            for data_type in sorted(interrupted_full_fetches(db, connection_id)):
                log_structured(
                    self.logger,
                    "warning",
                    "Polar full fetch interrupted",
                    provider="polar",
                    task="load_and_save_all",
                    action=POLAR_FULL_FETCH_INTERRUPTED,
                    data_type=data_type,
                    user_id=str(user_id),
                )
                record_full_end(db, connection_id, data_type)
                if data_type not in tasks:
                    results[data_type] = DataTypeFailure(code=POLAR_FULL_FETCH_INTERRUPTED)
        stopped: str | None = None
        for data_type, fn in tasks.items():
            if stopped is not None:
                # FORK (2.96.6.1, spec R1): Polar refused a call for its rate limit, so every later
                # call would be refused too, and still counted. Not asked; still due next pull.
                results[data_type] = DataTypeFailure(code=stopped)
                continue
            # FORK: a full fetch counts as failed until it ends (record_full_start), so a worker
            # killed in the middle of one is held back by the retry gap instead of being started
            # again by the next hourly pull.
            attempted_in_full = data_type in _FULL_FORM_TYPES and connection_id is not None
            if attempted_in_full:
                record_full_start(db, connection_id, data_type, now)
            try:
                results[data_type] = fn()
                db.commit()
            except Exception as e:
                db.rollback()
                # FORK (2.96.6.1, spec cause 3, AC5): a failed type is reported as failed, with a
                # code, so the sync records it as FAILED and does not move the cursor past it.
                # It used to read as 0 rows saved, i.e. "nothing new".
                code = error_code(e) or UNKNOWN
                results[data_type] = DataTypeFailure(code=code)
                # A code, never the exception's text, and no traceback (spec cause 3, R-2026-10-06-b).
                log_structured(
                    self.logger,
                    "warning",
                    "Failed to sync a Polar data type",
                    provider="polar",
                    task="load_and_save_all",
                    data_type=data_type,
                    user_id=str(user_id),
                    error_code=code,
                )
            outcome = results[data_type]
            if isinstance(outcome, DataTypeFailure):
                if outcome.code == RATE_LIMITED:
                    stopped = RATE_LIMITED
                # Spec cause 3, AC4 (v0.6): a full fetch that failed is asked for again after a
                # growing gap, at least once every 22 hours (pull_budget.record_full_failure). A
                # short form that failed needs nothing: the next pull asks for it again anyway,
                # and a night that is no longer the newest is the next full fetch's.
                # A type asked for in full has its failure recorded already, when it started.
                if attempted_in_full:
                    record_full_end(db, connection_id, data_type)
                elif data_type in _FULL_FORM_TYPES and connection_id is not None:
                    record_full_failure(db, connection_id, data_type, now)
            elif data_type in _FULL_FORM_TYPES and connection_id is not None:
                record_full_fetch(db, connection_id, data_type, now)

        if stopped is not None:
            log_structured(
                self.logger,
                "warning",
                "Polar pull stopped at the rate limit",
                provider="polar",
                task="load_and_save_all",
                user_id=str(user_id),
                error_code=stopped,
            )
        return results

    # -------------------------------------------------------------------------
    # Not implemented — Polar recovery and activity samples map to other modules
    # -------------------------------------------------------------------------

    def get_recovery_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        return []

    def normalize_recovery(
        self,
        raw_recovery: dict[str, Any],
        user_id: UUID,
    ) -> dict[str, Any]:
        return {}

    def get_activity_samples(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        return []

    def normalize_activity_samples(
        self,
        raw_samples: list[dict[str, Any]],
        user_id: UUID,
    ) -> dict[str, list[dict[str, Any]]]:
        return {}
