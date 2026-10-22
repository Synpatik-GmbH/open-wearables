"""Per-record parsing of an SDK sync payload, shared by the importer and the history gate."""

from typing import TypedDict

from pydantic import ValidationError

from app.schemas.providers.mobile_sdk import SyncRequest as SDKSyncRequest
from app.schemas.providers.mobile_sdk.sync_request import MetricRecord, SleepRecord, SyncRequestData, Workout

_SDK_ITEM_MODELS = (("records", MetricRecord), ("sleep", SleepRecord), ("workouts", Workout))


class InvalidRecord(TypedDict):
    """A single record dropped by per-record validation (PII-free — only the location, no values)."""

    collection: str  # "records" | "sleep" | "workouts"
    index: int
    loc: str
    msg: str | None
    type: str | None


def parse_sync_request(raw: dict) -> tuple[SDKSyncRequest, list[InvalidRecord]]:
    """Parse a raw payload into a SyncRequest, salvaging valid records when items fail.

    Fast path validates in one shot. On failure, valid records are kept and the per-item
    failures returned (PII-free: loc/msg/type). Raises ValidationError when the envelope or
    a container shape is invalid — nothing salvageable, so the batch is rejected (400).
    """
    try:
        return SDKSyncRequest(**raw), []
    except ValidationError:
        pass  # fall through to per-item validation

    # Only individual records are salvageable. A malformed container shape (data not a dict,
    # or a collection not a list) is not — re-validate to re-raise the real ValidationError.
    data = raw.get("data")
    if not isinstance(data, dict) or any(not isinstance(data.get(key, []), list) for key, _ in _SDK_ITEM_MODELS):
        return SDKSyncRequest(**raw), []

    kept: dict[str, list] = {}
    dropped: list[InvalidRecord] = []
    for key, model in _SDK_ITEM_MODELS:
        good: list = []
        for idx, item in enumerate(data.get(key, [])):
            try:
                good.append(model.model_validate(item))
            except ValidationError as exc:
                err = (exc.errors() or [{}])[0]
                dropped.append(
                    {
                        "collection": key,
                        "index": idx,
                        "loc": ".".join(str(x) for x in err.get("loc", [])),
                        "msg": err.get("msg"),
                        "type": err.get("type"),
                    }
                )
        kept[key] = good

    # Reconstruct via model_validate so Pydantic still rejects a bad envelope; `data` now
    # carries only the kept records.
    request = SDKSyncRequest.model_validate(
        {**raw, "data": SyncRequestData(records=kept["records"], sleep=kept["sleep"], workouts=kept["workouts"])}
    )
    return request, dropped
