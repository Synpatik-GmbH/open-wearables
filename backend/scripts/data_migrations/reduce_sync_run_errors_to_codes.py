#!/usr/bin/env python3
"""Reduce stored sync errors to codes (fork, Notion 2.47.17.2).

Until this fork tag a failed sync stored the raw text of its error in sync_run.error,
inside sync_run.meta, and in sync_run_data_type.error / error_code, and cached it in
the Redis sync history, which this script rewrites too. For a database error
that text quotes the values being saved and the user id. The write path now keeps a code
only (app/services/sync_error_code.py); this script rewrites the rows an older image
left behind, with the same rule, so a stored code is kept and anything else becomes
``unclassified``.

It prints how many rows it changed and never what they held.

Idempotent: a cleaned row no longer differs from its cleaned form, so re-runs are
no-ops. It runs on every API start and reads every row. A worker still on the older
image can write one more row after that; the periodic close_stale_sync_runs task makes
the same pass over recent rows for the first two hours of its worker process.

Two stores, handled apart. The database is rewritten and committed first; Redis is
touched only after that, so a Redis that cannot be reached does not undo the database
pass. A Redis failure still ends the script with an error, which the start-up script
reports as a warning and the next start retries. In the database a row is read again
under a row lock before it is rewritten; in Redis a key is rewritten under WATCH, and a
key a worker keeps writing to ends the pass after a few attempts instead of holding up
the API's start.

Neither of those can promise to come after the last write of an older image, so the
rollout that first carries this ends with one run by hand, after the last app has
moved (FORK-DELTA.md). A second run then reports nothing left.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/reduce_sync_run_errors_to_codes.py --dry-run
    docker compose exec app uv run python scripts/data_migrations/reduce_sync_run_errors_to_codes.py
"""

import argparse

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.services.sync_error_cleanup import reduce_stored_sync_errors
from app.services.sync_status_service import reduce_cached_sync_errors


def reduce_sync_run_errors(db: Session, *, dry_run: bool) -> dict[str, int]:
    """Rewrite every stored row and print the counts. Does not commit."""
    result = reduce_stored_sync_errors(db, dry_run=dry_run)
    verb = "Would reduce" if dry_run else "Reduced"
    print(f"sync_run:           {verb} {result['runs']} run(s) to an error code")
    print(f"sync_run_data_type: {verb} {result['data_types']} per-data-type row(s) to an error code")
    return result


def reduce_cached_events(*, dry_run: bool) -> int:
    """Rewrite the events cached in Redis and print the count."""
    cached = reduce_cached_sync_errors(dry_run=dry_run)
    verb = "Would reduce" if dry_run else "Reduced"
    print(f"Redis history:      {verb} {cached} cached event(s) to an error code")
    return cached


def main(dry_run: bool) -> None:
    # The database first, and committed before Redis is touched: the two stores are
    # independent, and a Redis that cannot be reached must not undo the database pass.
    # A Redis failure still ends the script with an error, so the cache pass is seen
    # to be owed and the next start retries it.
    with SessionLocal() as db:
        result = reduce_sync_run_errors(db, dry_run=dry_run)
        if not dry_run and any(result.values()):
            db.commit()

    cached = reduce_cached_events(dry_run=dry_run)

    if dry_run:
        print("\nDry run — no changes made.")
    elif not any(result.values()) and not cached:
        print("Nothing to do — no stored sync error held text.")
    else:
        print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Count affected rows without modifying data")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
