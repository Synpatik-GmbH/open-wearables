from datetime import datetime

from app.constants.sleep import SleepStageType
from app.schemas.model_crud.activities import SleepStage
from app.schemas.providers.mobile_sdk import SleepStateStage
from app.schemas.providers.mobile_sdk.sync_request import SleepRecord


def ordered_sleep_stages(stages: list[SleepRecord]) -> list[SleepRecord]:
    """Use the importer's existing identity and stable chronological order at both boundaries."""
    seen = set()
    unique = []
    for item in sorted(stages, key=lambda stage: stage.startDate):
        source_key = item.source.model_dump_json() if item.source else None
        identity = (item.startDate, item.endDate, item.stage, source_key)
        if identity not in seen:
            seen.add(identity)
            unique.append(item)
    return unique


_STAGE_TO_METRIC: dict[str, str] = {
    "awake": "awake_seconds",
    "sleeping": "sleeping_seconds",
    "light": "light_seconds",
    "deep": "deep_seconds",
    "rem": "rem_seconds",
}


def _calculate_final_metrics(stages: list[SleepStateStage]) -> tuple[dict, list[SleepStage]]:
    """
    Recalculate metrics from stages, handling overlaps by prioritizing earlier segments.
    Returns (metrics_dict, cleaned_stages_list).

    Input stages are now list[SleepStateStage] Pydantic models with normalized stage values.
    """
    metrics = {
        "in_bed_seconds": 0,
        "awake_seconds": 0,
        "sleeping_seconds": 0,
        "light_seconds": 0,
        "deep_seconds": 0,
        "rem_seconds": 0,
    }

    # Determine processing strategy based on what stage types are present:
    # - Only in_bed (no sleeping/light/deep/rem): treat in_bed as sleeping (legacy devices)
    # - Detailed phases present (light/deep/rem): use only detailed + awake, drop sleeping wrapper
    # - Only sleeping (no detailed): use sleeping + awake as-is
    has_detailed = any(s.stage in ("light", "deep", "rem") for s in stages)
    has_sleep_data = any(s.stage in ("sleeping", "light", "deep", "rem") for s in stages)

    if not has_sleep_data:
        processable = [
            SleepStateStage(stage=SleepStageType.SLEEPING, start_time=s.start_time, end_time=s.end_time)
            if s.stage == "in_bed"
            else s
            for s in stages
            if s.stage != "unknown"
        ]
    elif has_detailed:
        processable = [s for s in stages if s.stage not in ("in_bed", "sleeping", "unknown")]
    else:
        processable = [s for s in stages if s.stage not in ("in_bed", "unknown")]

    sorted_processable = sorted(processable, key=lambda x: x.start_time)

    cleaned_stages: list[SleepStage] = []
    last_end = None

    for stage in sorted_processable:
        start = stage.start_time
        end = stage.end_time

        if last_end and start < last_end:
            start = last_end

        if start >= end:
            continue

        duration = (end - start).total_seconds()

        # Safe to access .stage (Pydantic model)
        phase_str = str(stage.stage)

        metric_key = _STAGE_TO_METRIC.get(phase_str)
        if metric_key:
            metrics[metric_key] += duration

        cleaned_stages.append(SleepStage(stage=SleepStageType(phase_str), start_time=start, end_time=end))
        last_end = end

    # 2. Process IN_BED duration separately (union of intervals)
    in_bed_raw = [s for s in stages if s.stage == "in_bed"]
    if in_bed_raw:
        sorted_in_bed = sorted(in_bed_raw, key=lambda x: x.start_time)
        current_start = None
        current_end = None

        for stage in sorted_in_bed:
            start = stage.start_time
            end = stage.end_time

            if current_start is None:
                current_start = start
                current_end = end
                continue

            if start < current_end:  # ty:ignore[unsupported-operator]
                current_end = max(current_end, end)  # ty:ignore[invalid-argument-type]
            else:
                metrics["in_bed_seconds"] += (current_end - current_start).total_seconds()  # ty:ignore[unsupported-operator]
                current_start = start
                current_end = end

        if current_start and current_end:
            metrics["in_bed_seconds"] += (current_end - current_start).total_seconds()
    else:
        metrics["in_bed_seconds"] = (
            metrics["awake_seconds"]
            + metrics["sleeping_seconds"]
            + metrics["light_seconds"]
            + metrics["deep_seconds"]
            + metrics["rem_seconds"]
        )

    return metrics, cleaned_stages


def normalized_sleep_window(
    stages: list[SleepStateStage], raw_start: datetime, raw_end: datetime
) -> tuple[datetime, datetime]:
    """Use the unchanged finalizer's cleaned window, retaining legacy fallback bounds."""
    _, cleaned = _calculate_final_metrics(stages)
    return (cleaned[0].start_time, cleaned[-1].end_time) if cleaned else (raw_start, raw_end)
