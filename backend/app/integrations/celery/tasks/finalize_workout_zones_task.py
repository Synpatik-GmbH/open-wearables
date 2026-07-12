"""Celery debounce task that re-emits ``workout.created`` once the HR trace settles.

A ``workout.created`` webhook fires the instant Apple reports a workout, but the
heart-rate trace uploads in later chunks — so the Edwards zones computed at ingest are
partial. This task polls HR-trace completeness every ``workout_zone_debounce_seconds``
and, once :func:`should_emit` reports the trace has settled (stopped growing, reached
``workout_zone_target_completeness``, or hit ``workout_zone_hard_cap_seconds``), emits a
single authoritative ``workout.created`` carrying the final zones.

A Redis ``SETNX`` marker (``workout:{id}:emitted``) guarantees exactly one emit even if
two polls race; the emit itself is also idempotent at the Svix layer (keyed on the
record id), so a lost race can never duplicate the delivery.
"""

from __future__ import annotations

from datetime import datetime, timezone
from logging import getLogger
from typing import TYPE_CHECKING, cast
from uuid import UUID

from celery import shared_task

from app.config import settings
from app.database import SessionLocal
from app.integrations.redis_client import get_redis_client
from app.models import DataSource, EventRecord

if TYPE_CHECKING:
    from app.models import WorkoutDetails
from app.services.apple.healthkit.workout_settle import should_emit
from app.services.event_record_service import event_record_service

logger = getLogger(__name__)

_EMITTED_TTL_SECONDS = 3600


def _emitted_key(workout_id: str) -> str:
    return f"workout:{workout_id}:emitted"


@shared_task(
    name="app.integrations.celery.tasks.finalize_workout_zones_task.finalize_workout_zones",
    queue="webhook_sync",
)
def finalize_workout_zones(
    workout_id: str,
    prev_completeness: float | None = None,
    first_seen_iso: str | None = None,
) -> dict:
    """Poll a workout's HR-trace completeness and emit ``workout.created`` once settled.

    Returns a small status dict describing the outcome of this poll:
    ``already_emitted`` / ``missing_record`` / ``missing`` (skipped), ``rescheduled``
    (another poll queued), or ``emitted`` (settled — the winner fired the webhook).
    """
    redis = get_redis_client()
    if redis.get(_emitted_key(workout_id)):
        return {"emitted": False, "reason": "already_emitted"}

    with SessionLocal() as db:
        record = db.get(EventRecord, UUID(workout_id))
        if record is None or record.data_source_id is None:
            return {"emitted": False, "reason": "missing_record"}

        data_source = db.get(DataSource, record.data_source_id)
        detail = event_record_service.event_record_detail_repo.get_by_record_id(db, record.id)
        if data_source is None or detail is None:
            return {"emitted": False, "reason": "missing"}

        zone_minutes, completeness = event_record_service._compute_workout_hr_zone_fields(db, record, data_source)

        now = datetime.now(timezone.utc)
        first_seen = datetime.fromisoformat(first_seen_iso) if first_seen_iso else now
        elapsed = (now - first_seen).total_seconds()

        if should_emit(
            prev_completeness,
            completeness,
            elapsed,
            settings.workout_zone_target_completeness,
            settings.workout_zone_hard_cap_seconds,
        ):
            # Emit-once guard: only the poll that wins SETNX fires the webhook.
            if redis.set(_emitted_key(workout_id), "1", nx=True, ex=_EMITTED_TTL_SECONDS):
                # This task is only ever scheduled for workout records, so the persisted
                # detail is a WorkoutDetails; narrow the base repo return type for the checker.
                event_record_service._emit_workout_created_from_persisted(
                    db, record, data_source, cast("WorkoutDetails", detail), zone_minutes, completeness
                )
            return {"emitted": True, "completeness": completeness}

        # Still growing under both target and cap — poll again after the debounce window,
        # carrying this tick's completeness forward as the next "prev" and preserving the
        # original first-seen so the hard cap is measured from the first poll.
        finalize_workout_zones.apply_async(
            args=[workout_id],
            kwargs={"prev_completeness": completeness, "first_seen_iso": first_seen.isoformat()},
            countdown=settings.workout_zone_debounce_seconds,
        )
        return {"emitted": False, "reason": "rescheduled", "completeness": completeness}
