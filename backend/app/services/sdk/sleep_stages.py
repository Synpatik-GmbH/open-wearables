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
