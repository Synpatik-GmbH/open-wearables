"""2.96.9.2 (wearable history freeze purge): the scheduled temporary-store work.

D-02: no raw payload, SDK or FIT copy of any member is kept longer than seven days. The
sweep raises on a failed deletion, so the run shows as failed and the next one retries.
"""

from logging import getLogger

from celery import shared_task

from app.services.raw_payload_storage import sweep_expired_copies
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


@shared_task(name="app.integrations.celery.tasks.wearable_history_purge_task.sweep_expired_copies")
def sweep_expired_copies_task() -> dict:
    """Delete expired copies and log how many went; an error is not caught."""
    deleted = sweep_expired_copies()
    log_structured(logger, "info", "Expired temporary copies deleted", deleted=deleted)
    return {"deleted": deleted}
