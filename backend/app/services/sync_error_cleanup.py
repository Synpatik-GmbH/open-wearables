"""Reduce sync errors that are already stored to codes (fork, Notion 2.47.17.2).

The write path keeps a code only (sync_error_code.py). An image from before that stored
the error's text, and during a rolling deploy a worker on that image can store one more
row after everything else has moved on. This rewrites stored rows with the same rule.
"""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import SyncRun, SyncRunDataType
from app.repositories.sync_run_repository import sync_run_repository
from app.services.sync_error_code import error_code, without_error_text


def reduce_stored_sync_errors(db: Session, *, dry_run: bool = False, since: datetime | None = None) -> dict[str, int]:
    """Rewrite stored error text as codes, returning how many rows needed it.

    ``since`` limits the pass to rows updated from then on; without it every row is read.
    Does not commit — the caller owns the transaction, and the locks on rewritten rows are
    held until it ends.
    """
    # Two steps, because a worker can store a run's next event between a read and a
    # write. The first read only finds the rows that need rewriting. Each of those is
    # then read again under a row lock, and what is written comes from that second read.
    run_ids = [
        run.id
        for run in sync_run_repository.runs_with_an_error(db, since)
        if _run_as_codes(run) != (run.error, run.meta)
    ]
    row_keys = [
        (row.run_id, row.data_type)
        for row in sync_run_repository.data_types_with_an_error(db, since)
        if _row_as_codes(row) != (row.error, row.error_code)
    ]
    if dry_run:
        return {"runs": len(run_ids), "data_types": len(row_keys)}

    runs = 0
    for run_id in run_ids:
        run = sync_run_repository.lock_run(db, run_id)
        if run is None or _run_as_codes(run) == (run.error, run.meta):
            continue
        run.error, run.meta = _run_as_codes(run)
        runs += 1

    data_types = 0
    for run_id, data_type in row_keys:
        row = sync_run_repository.lock_data_type(db, run_id, data_type)
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
