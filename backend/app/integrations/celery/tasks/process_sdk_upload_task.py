import uuid
from logging import getLogger
from typing import Any, NoReturn
from uuid import UUID

from celery import Task, shared_task

from app.config import settings
from app.constants.sdk_providers import sdk_providers
from app.database import SessionLocal
from app.integrations.redis_client import get_redis_client
from app.models import User
from app.repositories.user_repository import UserRepository
from app.schemas.sync_status import (
    DataTypeKind,
    DataTypeOutcome,
    SyncScope,
    SyncSource,
    SyncStatus,
)
from app.services.raw_payload_storage import delete_payload_from_s3, get_payload_from_s3
from app.services.sdk.import_service import (
    ImportService as SDKImportService,
)
from app.services.sdk.import_service import (
    import_service as sdk_import_service,
)
from app.services.sync_error_code import SDK_IMPORT_FAILED, SDK_UPLOAD_DELIVERY_CAP, error_code
from app.services.sync_status_service import (
    emit_sync_completed,
    emit_sync_failed,
    emit_sync_started,
    try_record_data_types,
)
from app.services.user_connection_service import user_connection_service
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


def _get_import_service(provider: str) -> SDKImportService:
    if provider in sdk_providers():
        return sdk_import_service
    raise ValueError(f"Unsupported provider: {provider}")


def _batch_outcomes(types: list[str], workouts_saved: int, sleep_saved: int, scope: SyncScope) -> list[DataTypeOutcome]:
    """What this batch wrote, per data type.

    types holds series type slugs only, since the importer collects it from the samples it
    writes. Workouts and sleep are event records counted separately, so they would go
    unrecorded without their own entries.

    A historical export gets each type's verdict from the SDK's own end event, so a batch
    reports IN_PROGRESS and leaves the outcome to it — the two arrive in either order, and
    a batch claiming success would otherwise bury a reported failure. A live batch is the
    only report there will be, so it is its own verdict.
    """
    status = SyncStatus.IN_PROGRESS if scope == SyncScope.HISTORICAL else SyncStatus.SUCCESS
    outcomes = [DataTypeOutcome(data_type=data_type, kind=DataTypeKind.SERIES, status=status) for data_type in types]
    for name, saved in (("workouts", workouts_saved), ("sleep", sleep_saved)):
        if saved:
            outcomes.append(
                DataTypeOutcome(
                    data_type=name,
                    kind=DataTypeKind.EVENT,
                    status=status,
                    items_inserted=saved,
                )
            )
    return outcomes


# FORK (data protection, Notion 2.47.17.2): ignore_result, because a task that raises
# would have its exception, text and traceback, kept in the result backend for three
# days. Nothing reads this task's result.
#
# FORK (2.96.6.1, spec 3.3, Apple Health cause 3): bound and retried. The SDK holds its 202 for
# a batch and never re-sends it, so a batch whose import failed for a transient reason (an
# unexpected error, a busy sleep lock), or failed in a step before the import (the S3 load, the
# user check, the connection setup), is processed again from the same content or S3 payload,
# with backoff, before it is reported failed. Re-processing is safe: samples upsert, workouts
# and sleep are keyed on their times.
SDK_IMPORT_MAX_RETRIES = 5
SDK_IMPORT_RETRY_BASE_SECONDS = 30


# FORK (2.96.6.3, spec C1): the delivery cap. The task is acknowledged only when it ends, so a
# batch whose worker is killed in the middle is delivered again (by Kombu, once the visibility
# timeout has passed: an hour by default). A batch that itself kills the worker would then kill it
# on every delivery. So each delivery counts itself in Redis before doing anything, every exit that
# is not a kill resets the count, and a delivery that finds the three before it all killed is
# recorded failed instead of processed. In-task retries are not deliveries the worker died in: a
# scheduled retry resets the count like any other exit, so the 2.96.6.1 retries keep their five.
SDK_UPLOAD_MAX_DELIVERIES = 3
# Refreshed on every delivery, so the count outlives the wait for the next one. Twenty-four hours
# is the parent spec's bound for late data (2.96.6 AC2). The count is deleted when the batch ends,
# so the expiry only clears a count whose batch was lost from the broker.
SDK_UPLOAD_DELIVERY_TTL_SECONDS = 24 * 3600


def _delivery_key(batch_id: str) -> str:
    # The batch id and, as the value, a number: nothing else (spec section 8).
    return f"sdk_upload:deliveries:{batch_id}"


def _count_delivery(batch_id: str) -> int | None:
    """Count this delivery of the batch. None when the count cannot be kept.

    A batch whose count cannot be kept is processed, not refused: the cap protects the worker, and
    losing the batch is the failure this task exists to prevent.
    """
    key = _delivery_key(batch_id)
    try:
        pipe = get_redis_client().pipeline()
        pipe.incr(key)
        pipe.expire(key, SDK_UPLOAD_DELIVERY_TTL_SECONDS)
        count, _ = pipe.execute()
        return int(count)
    except Exception as e:
        log_structured(
            logger,
            "warning",
            "SDK batch delivery count unavailable; processing without the cap",
            action="sdk_upload_delivery_count",
            batch_id=batch_id,
            error=error_code(e),
        )
        return None


def _end_deliveries(batch_id: str) -> None:
    """The batch's delivery ended without a kill, so the count of killed deliveries starts again."""
    try:
        get_redis_client().delete(_delivery_key(batch_id))
    except Exception as e:
        log_structured(
            logger,
            "warning",
            "SDK batch delivery count not cleared",
            action="sdk_upload_delivery_count",
            batch_id=batch_id,
            error=error_code(e),
        )


def _retry_delay(retries: int) -> int:
    """Seconds before the next attempt: 30, then twice as long each time."""
    return SDK_IMPORT_RETRY_BASE_SECONDS * 2**retries


def _retry_or_fail_before_import(
    task: Task,
    error: Exception,
    user_uuid: UUID,
    provider: str,
    scope: SyncScope,
    run_id: str,
    batch_id: str,
) -> NoReturn:
    """FORK (2.96.6.1): retry a batch whose failure came before its import, while retries remain.

    With none left, the batch is recorded as failed, with the exception's code and not its text,
    and the exception is raised again.
    """
    if task.request.retries < task.max_retries:
        raise task.retry(countdown=_retry_delay(task.request.retries))
    emit_sync_failed(
        user_uuid,
        provider,
        SyncSource.SDK,
        scope=scope,
        run_id=run_id,
        error=SDK_IMPORT_FAILED,
        message=f"{provider.capitalize()} batch failed before its import",
        metadata={"batch_id": batch_id, "cause": error_code(error)},
    )
    raise error


# FORK (2.96.6.3, spec AC1): acks_late, so a batch is acknowledged when it ends rather than when
# a thread starts it, and a worker killed in the middle leaves it to be delivered again. On this
# task's worker (--pool=threads) reject_on_worker_lost has no effect: it acts when a prefork child
# dies. It is set so the task keeps the redelivery if it ever runs on a prefork worker. A batch that
# raises is still acknowledged (Celery's default), so only a kill brings a batch back.
@shared_task(
    bind=True,
    queue="sdk_sync",
    ignore_result=True,
    max_retries=SDK_IMPORT_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_sdk_upload(
    self: Task,
    content: str | None,
    content_type: str,
    user_id: str,
    provider: str,
    batch_id: str | None = None,
    payload_ref: str | None = None,
    sync_session_id: str | None = None,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """
    Process SDK data import asynchronously.

    Args:
        content: The request content as string (JSON or multipart data). None when the
            payload was offloaded to S3 - see ``payload_ref``.
        content_type: The content type header value
        user_id: User ID to associate with the data
        provider: Import provider - "apple", "samsung", "health_connect"
        batch_id: Unique batch identifier for tracking (optional for backwards compatibility)
        payload_ref: ``s3://bucket/key`` of the stored payload. When set (and ``content`` is
            None) the body is loaded from S3 here, so it never travels through the broker.
        sync_session_id: Device-generated id shared by every batch of one historical
            export. Absent on SDK versions that do not send it yet.
        sync_type: Whether this batch belongs to a historical export or to live sync.

    Returns:
        Dictionary with status_code and response message
    """
    # Generate batch_id if not provided (backwards compatibility). FORK (2.96.6.3): the task id
    # first, because it is the same on every delivery of the message, so the cap still counts a
    # batch queued without one.
    if not batch_id:
        batch_id = self.request.id or str(uuid.uuid4())

    # FORK (2.96.6.3, spec C1): counted before anything else, so a delivery that dies at any point
    # is counted. A kill ends the thread without running the lines below; every other exit does.
    deliveries = _count_delivery(batch_id)
    try:
        result = _process_sdk_upload(
            self,
            content,
            content_type,
            user_id,
            provider,
            batch_id,
            payload_ref,
            sync_session_id,
            sync_type,
            deliveries,
        )
    except Exception:
        # A scheduled retry (celery's Retry is an Exception) or an error: not a kill.
        _end_deliveries(batch_id)
        raise
    _end_deliveries(batch_id)
    return result


def _process_sdk_upload(
    task: Task,
    content: str | None,
    content_type: str,
    user_id: str,
    provider: str,
    batch_id: str,
    payload_ref: str | None,
    sync_session_id: str | None,
    sync_type: str | None,
    deliveries: int | None,
) -> dict[str, Any]:
    """One delivery of ``process_sdk_upload``, whose arguments it takes, plus this delivery's count."""
    # A historical export spans many batches, so its run is keyed by the device's session
    # id. Without one the batch is its own run, which is why live syncs are not persisted.
    scope = SyncScope.HISTORICAL if sync_type == SyncScope.HISTORICAL and sync_session_id else SyncScope.LIVE
    run_id = f"sdk_{sync_session_id}" if scope == SyncScope.HISTORICAL else batch_id

    # Validate user_id format
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        log_structured(
            logger,
            "warning",
            "Invalid user_id format",
            provider=provider,
            action="validate_user_id",
            batch_id=batch_id,
            user_id=user_id,
        )
        return {"status": "error", "reason": "invalid_user_id", "batch_id": batch_id}

    # FORK (2.96.6.3, spec C1, AC3): the three deliveries before this one were each killed before
    # they ended, so this batch is what kills the worker. Recorded failed, with codes only, and
    # acknowledged without loading or importing anything. The S3 payload is kept, as for any
    # failed batch.
    if deliveries is not None and deliveries > SDK_UPLOAD_MAX_DELIVERIES:
        log_structured(
            logger,
            "warning",
            f"{provider.capitalize()} batch refused after {SDK_UPLOAD_MAX_DELIVERIES} killed deliveries",
            provider=provider,
            action=SDK_UPLOAD_DELIVERY_CAP,
            batch_id=batch_id,
            user_id=user_id,
            deliveries=deliveries,
        )
        emit_sync_failed(
            user_uuid,
            provider,
            SyncSource.SDK,
            scope=scope,
            run_id=run_id,
            error=SDK_IMPORT_FAILED,
            message=f"{provider.capitalize()} batch refused after {SDK_UPLOAD_MAX_DELIVERIES} killed deliveries",
            metadata={"batch_id": batch_id, "cause": SDK_UPLOAD_DELIVERY_CAP},
        )
        return {"status": "error", "reason": SDK_UPLOAD_DELIVERY_CAP, "batch_id": batch_id}

    # FORK (2.96.6.1, spec 3.3): the SDK holds its 202 for this batch and never re-sends it, so a
    # failure in a step before the import is retried like a failed import. The S3 payload stays
    # where it is until the batch is saved, so a retry loads it again.
    try:
        # Payload was offloaded to S3, so the body never travelled through the broker.
        if content is None and payload_ref:
            try:
                content = get_payload_from_s3(payload_ref)
            except Exception:
                log_structured(
                    logger,
                    "error",
                    "Failed to load SDK payload from S3",
                    provider=provider,
                    action="load_payload_ref",
                    batch_id=batch_id,
                    user_id=user_id,
                    payload_ref=payload_ref,
                )
                raise

        if content is None:
            log_structured(
                logger,
                "warning",
                "No payload content or reference provided",
                provider=provider,
                action="validate_payload_present",
                batch_id=batch_id,
                user_id=user_id,
            )
            return {"status": "error", "reason": "missing_payload", "batch_id": batch_id}

        # Validate user exists before processing
        with SessionLocal() as db:
            user_repo = UserRepository(User)
            if not user_repo.get(db, user_uuid):
                log_structured(
                    logger,
                    "warning",
                    "Skipping import for non-existent user",
                    provider=provider,
                    action="validate_user_exists",
                    batch_id=batch_id,
                    user_id=user_id,
                )
                return {"status": "skipped", "reason": "user_not_found", "batch_id": batch_id}
    except Exception as e:
        _retry_or_fail_before_import(task, e, user_uuid, provider, scope, run_id, batch_id)

    # Log task start
    log_structured(
        logger,
        "info",
        f"{provider.capitalize()} sync batch processing started",
        action=f"{provider}_batch_processing_start",
        batch_id=batch_id,
        user_id=user_id,
        provider=provider,
    )

    emit_sync_started(
        user_uuid,
        provider,
        SyncSource.SDK,
        scope=scope,
        run_id=run_id,
        message=f"Processing {provider} SDK batch",
        metadata={"batch_id": batch_id},
    )

    with SessionLocal() as db:
        # Ensure SDK connection exists for this user (SDK-based, no OAuth tokens).
        # Goes through the service so a new/reactivated connection emits connection.created.
        try:
            user_connection_service.ensure_sdk_connection(db, user_uuid, provider)
        except Exception as e:
            _retry_or_fail_before_import(task, e, user_uuid, provider, scope, run_id, batch_id)

        # Select the appropriate import service based on source
        import_service = _get_import_service(provider)

        result = import_service.import_data_from_request(
            db, content, content_type, user_id, batch_id=batch_id
        ).model_dump()

        # Log processing completion with results
        log_structured(
            logger,
            "info",
            f"{provider.capitalize()} sync batch processing completed",
            action=f"{provider}_batch_processing_complete",
            batch_id=batch_id,
            user_id=user_id,
            provider=provider,
            status_code=result.get("status_code"),
            response=result.get("response"),
            # Include counts from result if available
            records_saved=result.get("records_saved", 0),
            workouts_saved=result.get("workouts_saved", 0),
            sleep_saved=result.get("sleep_saved", 0),
        )

        status_code = result.get("status_code", 200)
        records_saved = int(result.get("records_saved", 0) or 0)
        records_inserted = int(result.get("records_inserted", 0) or 0)
        records_updated = int(result.get("records_updated", 0) or 0)
        workouts_saved = int(result.get("workouts_saved", 0) or 0)
        sleep_saved = int(result.get("sleep_saved", 0) or 0)
        dropped_count = int(result.get("dropped_count", 0) or 0)
        types = result.get("types") or []
        items_total = records_saved + workouts_saved + sleep_saved

        if isinstance(status_code, int) and 200 <= status_code < 300:
            # Same wording as webhook_push_task; records_saved counts samples submitted.
            message = f"{provider.capitalize()} batch saved: {records_inserted} new, {records_updated} updated"
            if dropped_count:
                message += f" ({dropped_count} record(s) dropped by validation)"
            emit_sync_completed(
                user_uuid,
                provider,
                SyncSource.SDK,
                scope=scope,
                run_id=run_id,
                status=SyncStatus.SUCCESS,
                message=message,
                items_processed=items_total,
                metadata={
                    "batch_id": batch_id,
                    "records_saved": records_saved,
                    "inserted": records_inserted,
                    "updated": records_updated,
                    "workouts_saved": workouts_saved,
                    "sleep_saved": sleep_saved,
                    "types": types,
                    "dropped_count": dropped_count,
                },
            )
            try_record_data_types(run_id, _batch_outcomes(types, workouts_saved, sleep_saved, scope), scope=scope)
            if payload_ref and settings.raw_payload_storage == "disabled":
                # Transport-only copy and the data is committed, so drop it. A failed batch
                # keeps its payload for diagnosis.
                delete_payload_from_s3(payload_ref)
        else:
            if result.get("retryable") and task.request.retries < task.max_retries:
                raise task.retry(countdown=_retry_delay(task.request.retries))
            emit_sync_failed(
                user_uuid,
                provider,
                SyncSource.SDK,
                scope=scope,
                run_id=run_id,
                error=SDK_IMPORT_FAILED,
                message=f"{provider.capitalize()} batch failed",
                metadata={"batch_id": batch_id, "status_code": status_code},
            )

        return {**result, "batch_id": batch_id}
