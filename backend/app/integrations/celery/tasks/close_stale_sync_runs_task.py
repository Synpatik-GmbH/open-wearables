from datetime import datetime, timedelta, timezone
from logging import getLogger

from celery import shared_task

from app.config import settings
from app.database import SessionLocal
from app.repositories.sync_run_repository import sync_run_repository
from app.services.sync_error_cleanup import reduce_stored_sync_errors
from app.services.sync_status_service import last_event_at
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

# FORK (data protection, Notion 2.47.17.2): how far back each sweep looks for a stored
# error that is still text. Rows older than this are the start-up script's, which reads
# every row; the sweep only has to catch what an older worker wrote since.
ERROR_CLEANUP_WINDOW = timedelta(hours=24)


@shared_task
def close_stale_sync_runs() -> dict:
    """Close sync runs that stopped reporting without an outcome.

    Postgres only sees a run's start and its terminal event, so one whose worker died stays
    in progress forever. Age alone would also catch a long backfill still working, so
    candidates are checked against Redis first: anything that emitted after the cutoff is
    alive and left alone.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings.sync_run_stale_after_hours)

    with SessionLocal() as db:
        # FORK (data protection, Notion 2.47.17.2): first, so that no early return skips it.
        errors_reduced = reduce_stored_sync_errors(db, since=now - ERROR_CLEANUP_WINDOW)
        # Always, not only when something was rewritten: it also releases the row locks.
        db.commit()
        if any(errors_reduced.values()):
            log_structured(
                logger,
                "warning",
                "Reduced stored sync errors to codes",
                action="sync_run_errors_reduced",
                runs=errors_reduced["runs"],
                data_types=errors_reduced["data_types"],
            )

        candidates = sync_run_repository.find_stale(db, cutoff)
        if not candidates:
            return {"closed_count": 0, "run_keys": [], "still_active": 0, "errors_reduced": errors_reduced}

        last_seen = last_event_at(candidates)
        if last_seen is None:
            # Without Redis every candidate looks dead, so skip rather than close them.
            return {
                "closed_count": 0,
                "run_keys": [],
                "still_active": len(candidates),
                "skipped": True,
                "errors_reduced": errors_reduced,
            }

        stale = [
            key for key in candidates if (last_seen.get(key) or datetime.min.replace(tzinfo=timezone.utc)) < cutoff
        ]
        closed = sync_run_repository.close_as_stale(db, stale, now)

    if closed:
        log_structured(
            logger,
            "warning",
            f"Closed {len(closed)} stale sync run(s)",
            action="sync_run_sweep_complete",
            stale_after_hours=settings.sync_run_stale_after_hours,
            closed_count=len(closed),
            still_active=len(candidates) - len(stale),
            run_keys=closed,
        )

    return {
        "closed_count": len(closed),
        "run_keys": closed,
        "still_active": len(candidates) - len(stale),
        "errors_reduced": errors_reduced,
    }
