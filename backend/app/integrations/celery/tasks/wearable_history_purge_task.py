"""2.96.9.2 (wearable history freeze purge): the scheduled temporary-store work.

D-02: no raw payload, SDK or FIT copy of any member is kept longer than seven days. The
sweep raises on a failed deletion, so the run shows as failed and the next one retries.
D-03: every due source's purge runs; each failed source is logged by the service and stays due.
D-04: processed XML upload files of frozen members are deleted, and abandoned uploads aborted.
"""

from logging import getLogger

from celery import shared_task

from app.database import SessionLocal
from app.services.apple.apple_xml.aws_service import get_s3_client
from app.services.apple.apple_xml.xml_upload_lifecycle import (
    abort_stale_multipart_uploads,
    delete_processed_files,
    fail_stale_imports,
)
from app.services.raw_payload_storage import sweep_expired_copies
from app.services.wearable_history_purge_service import wearable_history_purge_service
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


@shared_task(name="app.integrations.celery.tasks.wearable_history_purge_task.sweep_expired_copies")
def sweep_expired_copies_task() -> dict:
    """Delete expired copies and log how many went; an error is not caught."""
    deleted = sweep_expired_copies()
    log_structured(logger, "info", "Expired temporary copies deleted", deleted=deleted)
    return {"deleted": deleted}


@shared_task(name="app.integrations.celery.tasks.wearable_history_purge_task.purge_frozen_sources")
def purge_frozen_sources_task() -> dict:
    """Run every due source's purge; one source failing never stops the others."""
    with SessionLocal() as db:
        run = wearable_history_purge_service.run(db)
    log_structured(
        logger,
        "info",
        "Wearable history purge run",
        due=run.due,
        completed=run.completed,
        failed=run.failed,
        waiting=run.waiting,
    )
    return {"due": run.due, "completed": run.completed, "failed": run.failed, "waiting": run.waiting}


@shared_task(name="app.integrations.celery.tasks.wearable_history_purge_task.clean_xml_uploads")
def clean_xml_uploads_task() -> dict:
    """Fail stale imports, delete processed files of frozen members, abort abandoned uploads.

    Each failed object is logged where it fails and stays for the next run; nothing raises,
    because the purge of the member's Apple source waits on these rows (D-03).
    """
    s3 = get_s3_client()
    with SessionLocal() as db:
        stale = fail_stale_imports(db)
        deleted, failed = delete_processed_files(db, s3) if s3 is not None else (0, 0)
    aborted, abort_failed = abort_stale_multipart_uploads(s3) if s3 is not None else (0, 0)
    result = {
        "stale": stale,
        "deleted": deleted,
        "failed": failed,
        "aborted": aborted,
        "abort_failed": abort_failed,
    }
    log_structured(
        logger,
        "info",
        "XML upload files cleaned",
        provider="apple_xml",
        stale=stale,
        deleted=deleted,
        failed=failed,
        aborted=aborted,
        abort_failed=abort_failed,
    )
    return result
