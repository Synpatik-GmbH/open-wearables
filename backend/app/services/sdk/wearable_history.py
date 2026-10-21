from logging import getLogger
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sentry_sdk
from sqlalchemy.orm import Session

from app.config import settings
from app.constants.series_types.sdk import get_apple_sleep_phase, get_series_type_from_metric_type
from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository
from app.schemas.providers.mobile_sdk import SLEEP_START_STATES, SleepStateStage
from app.services.sdk.sleep_stages import normalized_sleep_window, ordered_sleep_stages
from app.utils.sleep_interval import interval_gap
from app.utils.structured_logging import log_structured

if TYPE_CHECKING:
    from sentry_sdk._types import Event, Hint

logger = getLogger(__name__)


def _without_sdk_request(event: "Event", _hint: "Hint") -> "Event":
    # FastAPI attaches the original unsanitized body before scope-local processors run.
    event.pop("request", None)
    return event


def sanitize_sdk_payload(db: Session, member: UUID, raw: dict, state: Any = None) -> dict:
    """Run before payload persistence and again on worker delivery. Provisional imports stay whole."""
    gate = WearableHistoryIngressRepository(db)
    provider = raw.get("provider")
    if not isinstance(provider, str):
        return raw  # Envelope validation remains with the importer; no records are persisted.
    source = gate.source((member, provider))
    if source is None or gate.now < source.freeze_at:
        return raw
    from app.services.sdk.import_service import _parse_sync_request

    request, dropped = _parse_sync_request(raw)
    if dropped:
        # The frozen transport cannot retain invalid raw items for worker diagnostics.
        # Report their locations/count here; an already-cleaned worker delivery is quiet.
        errors = [{key: item[key] for key in ("collection", "index", "loc", "type")} for item in dropped[:20]]
        with sentry_sdk.push_scope() as scope:
            scope.add_event_processor(_without_sdk_request)
            scope.set_level("warning")
            scope.set_context(
                "dropped_records", {"provider": provider, "dropped_count": len(dropped), "errors": errors}
            )
            sentry_sdk.capture_message(f"{provider} SDK payload: dropped invalid records, kept the rest")
        log_structured(
            logger,
            "warning",
            "SDK validation rejected records before frozen transport",
            provider=provider,
            action=f"{provider}_sdk_records_dropped",
            dropped_count=len(dropped),
        )
    intervals = []
    if state is not None and state.provider == provider:
        intervals.append(
            SimpleNamespace(
                category="sleep",
                user_id=member,
                provider=provider,
                start_datetime=state.start_time,
                end_datetime=state.end_time,
                zone_offset=state.zone_offset,
                stages=list(state.stages),
            )
        )
    stages = ordered_sleep_stages(request.data.sleep)
    for item in stages:
        phase = get_apple_sleep_phase(item.stage)
        if phase is None or item.endDate <= item.startDate:
            continue
        near = next(
            (
                r
                for r in reversed(intervals)
                if interval_gap(item.startDate, item.endDate, r.start_datetime, r.end_datetime)
                <= settings.sleep_end_gap_minutes * 60
            ),
            None,
        )
        stage = SleepStateStage(stage=phase.value, start_time=item.startDate, end_time=item.endDate)
        if near is not None:
            near.stages.append(stage)
            near.start_datetime = min(near.start_datetime, item.startDate)
            near.end_datetime = max(near.end_datetime, item.endDate)
            if item.zoneOffset and not near.zone_offset:
                near.zone_offset = item.zoneOffset
        elif phase in SLEEP_START_STATES:
            intervals.append(
                SimpleNamespace(
                    category="sleep",
                    user_id=member,
                    provider=provider,
                    start_datetime=item.startDate,
                    end_datetime=item.endDate,
                    zone_offset=item.zoneOffset,
                    stages=[stage],
                )
            )
    normalized_intervals = []
    admitted_intervals = []
    for raw_interval in intervals:
        start, end = normalized_sleep_window(
            raw_interval.stages, raw_interval.start_datetime, raw_interval.end_datetime
        )
        normalized = SimpleNamespace(**{**vars(raw_interval), "start_datetime": start, "end_datetime": end})
        normalized_intervals.append(normalized)
        if gate.event_admitted(normalized):
            admitted_intervals.append(raw_interval)
    # Keep wrappers of an admitted night for the existing in-bed arithmetic; only
    # its cleaned window supplies night placement and physiology association.
    request.data.sleep = [
        s
        for s in stages
        if any(r.start_datetime <= s.startDate and s.endDate <= r.end_datetime for r in admitted_intervals)
    ]
    request.data.workouts = [
        w
        for w in request.data.workouts
        if gate.event_admitted(
            SimpleNamespace(
                category="workout",
                user_id=member,
                provider=provider,
                start_datetime=w.startDate,
                end_datetime=w.endDate,
                zone_offset=w.zoneOffset,
            )
        )
    ]
    # These opaque extensions have no normalized consumers. They can contain additional
    # dated observations, so they cannot bypass the frozen record admission boundary.
    for workout in request.data.workouts:
        workout.samples = workout.route = workout.segments = workout.laps = workout.metadata = None
    for sleep in request.data.sleep:
        sleep.values = sleep.metadata = None
    candidates = []
    for item in request.data.records:
        metric = get_series_type_from_metric_type(item.type or "")
        if metric is None:
            continue
        sample = SimpleNamespace(
            user_id=member,
            provider=provider,
            series_type=metric,
            recorded_at=item.startDate,
            zone_offset=item.zoneOffset,
        )
        candidates.append((item, sample))
    gate.prime_samples([sample for _, sample in candidates])
    kept = []
    for item, sample in candidates:
        if gate.sample_admitted(sample, normalized_intervals):
            if getattr(sample.series_type, "value", sample.series_type) != "weight":
                item.metadata = None
            kept.append(item)
    request.data.records = kept
    # At the frozen boundary, unknown payload fields cannot carry unclassified raw history.
    return request.model_dump(mode="json", by_alias=True)
