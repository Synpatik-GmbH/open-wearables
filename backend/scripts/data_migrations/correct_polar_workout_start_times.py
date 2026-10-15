#!/usr/bin/env python3
"""Move Polar workouts stored two offsets late to their true start instant, each exactly once.

FORK (2.96.6.1, spec L2, AC15, AC16; ruling R-2026-10-13-e).

The problem. Polar sends an exercise's ``start_time`` as local wall-clock time with no zone
and ``start_time_utc_offset`` in minutes. Until 2.96.6.1 ``PolarWorkouts`` ADDED the offset
instead of subtracting it and handed Postgres a naive value, which it read in the session
time zone. With the session in UTC (the server default), every stored Polar workout sits at
``true + 2 x offset``: a 10:25 Berlin-summer run is stored at 12:25Z instead of 08:25Z, and a
late-evening workout lands on the next day.

What it changes. For every Polar workout (``data_source.provider = 'polar'``,
``event_record.category = 'workout'``) that has NO ``polar_workout_true_time`` row:
``start_datetime`` and ``end_datetime`` each move back by ``2 x zone_offset``. The stored
``zone_offset`` is the same offset Polar sent, written per row as ``+HH:MM`` / ``-HH:MM``.
``duration_seconds`` is unchanged (both ends move together). In the same transaction each
corrected row gets a ``polar_workout_true_time`` row with ``recorded_by = 'correction'``.
The wrong time is overwritten, not kept anywhere (R-2026-10-13-e). No webhook is sent: the
adapter corrects its own copies against what this leaves in place.

Exactly once. A Polar workout is a candidate only while it has no ``polar_workout_true_time``
row. Every workout the fixed code ingests is recorded at ingest, and every workout this
corrects is recorded here, so a second run finds no candidate and changes nothing.

Stops before changing anything (2.96.6.1 hard rule) when any of these counts is non-zero,
and reports every count, so a dry run tells the operator whether the run will go ahead:

- ``null_zone_offset``: a candidate with no offset, so its true time cannot be recomputed;
- ``unreadable_zone_offset``: an offset that is not ``+HH:MM`` / ``-HH:MM``;
- ``older_than_90_days``: a candidate whose true start falls on a Berlin date more than 90 days
  back (Q5: day 90 is inside the window, at any hour);
- ``collisions``: a candidate whose corrected (data source, start, end) is already held by
  another row, plus every candidate that corrects to the same (data source, start, end) as
  another candidate (two pre-fix copies of one exercise stored under different offsets);
  the unique index would reject either half-way;
- a session time zone other than UTC: the arithmetic above is exact only if the rows were
  written in a UTC session, and a non-UTC default now is the sign that they were not.

Not wired into ``scripts/start/app.sh``: an operator runs it once, after a dry run, as steps
4 and 5 of the 2.96.6.1 merge order. Run it BEFORE the adapter's correction, which corrects
its own copies only once this has.

Usage (inside the app container):
    uv run python scripts/data_migrations/correct_polar_workout_start_times.py --dry-run
    uv run python scripts/data_migrations/correct_polar_workout_start_times.py
"""

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal

_WINDOW_DAYS = 90
_OFFSET_SHAPE = r"^[+-][0-9]{2}:[0-9]{2}$"

# Polar workouts not yet known to hold their true instant.
_CANDIDATES = """
    SELECT er.id, er.data_source_id, er.start_datetime, er.end_datetime, er.zone_offset
    FROM event_record er
    JOIN data_source ds ON ds.id = er.data_source_id
    WHERE ds.provider = 'polar'
      AND er.category = 'workout'
      AND NOT EXISTS (SELECT 1 FROM polar_workout_true_time t WHERE t.event_record_id = er.id)
"""

# Candidates whose offset is readable, with the corrected times.
_CORRECTABLE = f"""
    SELECT c.id,
           c.data_source_id,
           c.start_datetime - 2 * CAST(c.zone_offset AS interval) AS true_start,
           c.end_datetime - 2 * CAST(c.zone_offset AS interval) AS true_end
    FROM ({_CANDIDATES}) c
    WHERE c.zone_offset ~ '{_OFFSET_SHAPE}'
"""

_COUNTS = text(f"""
    SELECT
      (SELECT count(*) FROM ({_CANDIDATES}) c) AS candidates,
      (SELECT count(*) FROM ({_CANDIDATES}) c WHERE c.zone_offset IS NULL) AS null_zone_offset,
      (SELECT count(*) FROM ({_CANDIDATES}) c
        WHERE c.zone_offset IS NOT NULL AND c.zone_offset !~ '{_OFFSET_SHAPE}') AS unreadable_zone_offset,
      (SELECT count(*) FROM ({_CORRECTABLE}) k
        WHERE (k.true_start AT TIME ZONE 'Europe/Berlin')::date
              < (now() AT TIME ZONE 'Europe/Berlin')::date - {_WINDOW_DAYS}) AS older_than_90_days,
      (SELECT count(*) FROM ({_CORRECTABLE}) k
        WHERE EXISTS (SELECT 1 FROM event_record o
                       WHERE o.data_source_id = k.data_source_id
                         AND o.start_datetime = k.true_start
                         AND o.end_datetime = k.true_end
                         AND o.id <> k.id))
      + (SELECT CAST(coalesce(sum(g.n), 0) AS bigint) FROM (
          SELECT count(*) AS n FROM ({_CORRECTABLE}) k
          GROUP BY k.data_source_id, k.true_start, k.true_end
          HAVING count(*) > 1) g) AS collisions,
      current_setting('TimeZone') AS session_time_zone
""")

_UPDATE = text(f"""
    UPDATE event_record er
    SET start_datetime = k.true_start,
        end_datetime = k.true_end
    FROM ({_CORRECTABLE}) k
    WHERE er.id = k.id
    RETURNING er.id
""")

_RECORD = text("""
    INSERT INTO polar_workout_true_time (event_record_id, recorded_by)
    SELECT unnest(CAST(:ids AS uuid[])), 'correction'
""")

_UTC_NAMES = frozenset({"UTC", "Etc/UTC", "Etc/UCT", "UCT", "GMT", "Etc/GMT", "Zulu", "Etc/Zulu"})


@dataclass
class CorrectionReport:
    candidates: int
    null_zone_offset: int
    unreadable_zone_offset: int
    older_than_90_days: int
    collisions: int
    session_time_zone: str
    corrected: int = 0

    @property
    def stopped(self) -> bool:
        return bool(
            self.null_zone_offset
            or self.unreadable_zone_offset
            or self.older_than_90_days
            or self.collisions
            or self.session_time_zone not in _UTC_NAMES
        )


def correct_polar_workout_start_times(db: Session, *, dry_run: bool) -> CorrectionReport:
    """Correct every candidate, or none. Commits only when it corrected something."""
    row = db.execute(_COUNTS).mappings().one()
    report = CorrectionReport(**dict(row))
    print(
        "polar workouts without a true-time record: "
        f"candidates={report.candidates} null_zone_offset={report.null_zone_offset} "
        f"unreadable_zone_offset={report.unreadable_zone_offset} "
        f"older_than_90_days={report.older_than_90_days} collisions={report.collisions} "
        f"session_time_zone={report.session_time_zone}"
    )
    if report.stopped:
        print("STOPPED: nothing changed. Every count above except candidates must be 0, and the time zone UTC.")
        return report
    if dry_run:
        print(f"Dry run: would correct {report.candidates} workout(s). Nothing changed.")
        return report
    if not report.candidates:
        print("Nothing to do.")
        return report

    ids = [r[0] for r in db.execute(_UPDATE)]
    db.execute(_RECORD, {"ids": ids})
    db.commit()
    report.corrected = len(ids)
    print(f"Corrected {report.corrected} workout(s).")
    return report


def main(dry_run: bool) -> int:
    with SessionLocal() as db:
        report = correct_polar_workout_start_times(db, dry_run=dry_run)
    return 2 if report.stopped else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report the counts without modifying data")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
