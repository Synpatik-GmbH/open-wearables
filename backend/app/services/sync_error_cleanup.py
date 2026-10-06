"""Reduce sync errors that are already stored to codes (fork, Notion 2.47.17.2).

The write path keeps a code only (sync_error_code.py). An image from before that stored
the error's text, and during a rolling deploy a worker on that image can store one more
row after everything else has moved on. This rewrites stored rows with the same rule.
"""

from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import SyncRun, SyncRunDataType
from app.services.sync_error_code import error_code, without_error_text


def reduce_stored_sync_errors(db: Session, *, dry_run: bool = False, since: datetime | None = None) -> dict[str, int]:
    """Rewrite stored error text as codes, returning how many rows needed it.

    ``since`` limits the pass to rows updated from then on; without it every row is read.
    Does not commit — the caller owns the transaction.
    """
    run_query = db.query(SyncRun).filter(or_(SyncRun.error.isnot(None), SyncRun.meta.isnot(None)))
    row_query = db.query(SyncRunDataType).filter(
        or_(SyncRunDataType.error.isnot(None), SyncRunDataType.error_code.isnot(None)),
    )
    if since is not None:
        run_query = run_query.filter(SyncRun.updated_at >= since)
        row_query = row_query.filter(SyncRunDataType.updated_at >= since)

    runs = 0
    for run in run_query:
        error, meta = error_code(run.error), without_error_text(run.meta)
        if (error, meta) == (run.error, run.meta):
            continue
        runs += 1
        if not dry_run:
            run.error, run.meta = error, meta

    data_types = 0
    for row in row_query:
        error, code = error_code(row.error), error_code(row.error_code)
        if (error, code) == (row.error, row.error_code):
            continue
        data_types += 1
        if not dry_run:
            row.error, row.error_code = error, code

    if not dry_run:
        db.flush()
    return {"runs": runs, "data_types": data_types}
