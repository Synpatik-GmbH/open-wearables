"""Reduce sync errors that are already stored to codes (fork, Notion 2.47.17.2).

The write path keeps a code only (sync_error_code.py). An image from before that stored
the error's text, and during a rolling deploy a worker on that image can store one more
row after everything else has moved on. This rewrites stored rows with the same rule.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import SyncRun, SyncRunDataType
from app.services.sync_error_code import error_code, without_error_text


def reduce_stored_sync_errors(db: Session, *, dry_run: bool = False, since: datetime | None = None) -> dict[str, int]:
    """Rewrite stored error text as codes, returning how many rows needed it.

    ``since`` limits the pass to rows updated from then on; without it every row is read.
    Does not commit — the caller owns the transaction, and the locks on rewritten rows are
    held until it ends.
    """
    run_query = db.query(SyncRun).filter(or_(SyncRun.error.isnot(None), SyncRun.meta.isnot(None)))
    row_query = db.query(SyncRunDataType).filter(
        or_(SyncRunDataType.error.isnot(None), SyncRunDataType.error_code.isnot(None)),
    )
    if since is not None:
        run_query = run_query.filter(SyncRun.updated_at >= since)
        row_query = row_query.filter(SyncRunDataType.updated_at >= since)

    # Two steps, because a worker can store a run's next event between a read and a
    # write. The first read only finds the rows that need rewriting. Each of those is
    # then read again under a row lock, and what is written comes from that second read.
    run_ids = [run.id for run in run_query if _run_as_codes(run) != (run.error, run.meta)]
    row_keys = [(row.run_id, row.data_type) for row in row_query if _row_as_codes(row) != (row.error, row.error_code)]
    if dry_run:
        return {"runs": len(run_ids), "data_types": len(row_keys)}

    runs = 0
    for run_id in run_ids:
        run = db.query(SyncRun).filter(SyncRun.id == run_id).with_for_update().populate_existing().one_or_none()
        if run is None or _run_as_codes(run) == (run.error, run.meta):
            continue
        run.error, run.meta = _run_as_codes(run)
        runs += 1

    data_types = 0
    for run_id, data_type in row_keys:
        row = (
            db.query(SyncRunDataType)
            .filter(SyncRunDataType.run_id == run_id, SyncRunDataType.data_type == data_type)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
        if row is None or _row_as_codes(row) == (row.error, row.error_code):
            continue
        row.error, row.error_code = _row_as_codes(row)
        data_types += 1

    db.flush()
    return {"runs": runs, "data_types": data_types}


def _run_as_codes(run: SyncRun) -> tuple[str | None, dict[str, Any] | None]:
    return error_code(run.error), without_error_text(run.meta)


def _row_as_codes(row: SyncRunDataType) -> tuple[str | None, str | None]:
    return error_code(row.error), error_code(row.error_code)
