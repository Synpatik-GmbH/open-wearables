#!/usr/bin/env python3
"""Reduce stored sync errors to codes (fork, Notion 2.47.17.2).

Until this fork tag a failed sync stored the raw text of its error in sync_run.error,
inside sync_run.meta, and in sync_run_data_type.error / error_code. For a database error
that text quotes the values being saved and the user id. The write path now keeps a code
only (app/services/sync_error_code.py); this script rewrites the rows an older image
left behind, with the same rule, so a stored code is kept and anything else becomes
``unclassified``.

It prints how many rows it changed and never what they held.

Idempotent: a cleaned row no longer differs from its cleaned form, so re-runs are
no-ops. It runs on every startup, because during a rolling deploy a worker still on the
older image can write one more row after the API has started.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/reduce_sync_run_errors_to_codes.py --dry-run
    docker compose exec app uv run python scripts/data_migrations/reduce_sync_run_errors_to_codes.py
"""

import argparse

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import SyncRun, SyncRunDataType
from app.services.sync_error_code import error_code, without_error_text


def reduce_sync_run_errors(db: Session, *, dry_run: bool) -> dict[str, int]:
    """Rewrite error text as codes. Does not commit — caller owns the transaction."""
    runs = 0
    for run in db.query(SyncRun).filter(or_(SyncRun.error.isnot(None), SyncRun.meta.isnot(None))):
        error, meta = error_code(run.error), without_error_text(run.meta)
        if (error, meta) == (run.error, run.meta):
            continue
        runs += 1
        if not dry_run:
            run.error, run.meta = error, meta

    data_types = 0
    for row in db.query(SyncRunDataType).filter(
        or_(SyncRunDataType.error.isnot(None), SyncRunDataType.error_code.isnot(None)),
    ):
        error, code = error_code(row.error), error_code(row.error_code)
        if (error, code) == (row.error, row.error_code):
            continue
        data_types += 1
        if not dry_run:
            row.error, row.error_code = error, code

    if not dry_run:
        db.flush()
    verb = "Would reduce" if dry_run else "Reduced"
    print(f"sync_run:           {verb} {runs} run(s) to an error code")
    print(f"sync_run_data_type: {verb} {data_types} per-data-type row(s) to an error code")
    if dry_run:
        print("\nDry run — no changes made.")
    return {"runs": runs, "data_types": data_types}


def main(dry_run: bool) -> None:
    with SessionLocal() as db:
        result = reduce_sync_run_errors(db, dry_run=dry_run)
        if dry_run:
            return
        if not any(result.values()):
            print("Nothing to do — no stored sync error held text.")
            return
        db.commit()
        print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Count affected rows without modifying data")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
