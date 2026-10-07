"""FORK (data protection, Notion 2.47.17.1): stored sync runs are kept for a period.

A run is of no use once its sync is done, so it is not kept for as long as the account
lives. Once a day every run stored longer ago than ``sync_run_retention_days`` is removed,
and its per-data-type rows go with it (``ON DELETE CASCADE``).
"""

from datetime import datetime, timedelta, timezone
from logging import getLogger

from celery import shared_task

from app.config import settings
from app.database import SessionLocal
from app.repositories.sync_run_repository import sync_run_repository
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


@shared_task(name="app.integrations.celery.tasks.prune_sync_runs_task.prune_old_sync_runs")
def prune_old_sync_runs() -> dict:
    """Remove the runs stored before the period, and say what is left.

    The line is written on every run, also when nothing was removed: it is what shows the
    task ran, and the age of the oldest run left is what shows the period is being kept.
    An error is not caught, so a run that could not remove anything shows as failed.
    """
    now = datetime.now(timezone.utc)
    retention_days = settings.sync_run_retention_days

    with SessionLocal() as db:
        removed_count = sync_run_repository.delete_stored_before(db, now - timedelta(days=retention_days))
        oldest = sync_run_repository.oldest_stored_at(db)

    oldest_remaining_age_days = None if oldest is None else round((now - oldest) / timedelta(days=1), 1)
    log_structured(
        logger,
        "info",
        "Removed the sync runs stored before the retention period",
        action="sync_run_prune_complete",
        removed_count=removed_count,
        retention_days=retention_days,
        oldest_remaining_age_days=oldest_remaining_age_days,
    )
    return {
        "removed_count": removed_count,
        "retention_days": retention_days,
        "oldest_remaining_age_days": oldest_remaining_age_days,
    }
