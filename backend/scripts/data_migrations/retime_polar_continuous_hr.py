#!/usr/bin/env python3
"""Store one dev account's Polar heart rate again at its true instants, once.

FORK (2.96.8, spec v1.1 AC9; ruling R-2026-10-21-c).

The problem. Until 2.96.8 Polar continuous heart rate was stored one UTC offset late (see
``Polar247Data.normalize_continuous_hr``). The fixed code stores each sample at its true
instant, but a sample already stored at the wrong one stays there, and nothing tells it apart
from the sleep record's own heart-rate samples, which are stored right: same source, same
series type. So the rows cannot be corrected in place; they are replaced from Polar.

What it does, for the account ``POLAR_HR_RETIME_USER_ID`` names and nothing else:

1. Asks Polar for its sleep listing, the continuous heart rate of the window the pull reads
   (``_RANGE_WINDOW_DAYS["continuous_hr"]``), and each listed night that is stored here. Every
   call is made before anything is written; a failing call changes nothing.
2. In one transaction: deletes the account's Polar heart-rate rows recorded from the first day
   of that window on, stores the continuous heart rate at its true instants (the fixed code,
   offsets read at ingest, nothing new stored) and each stored night's own samples again, and
   records that it ran. Sleep records, their scores and every other row are not touched. No
   webhook is sent: the adapter's ``rhr-coverage`` recompute with ``?days=28`` re-reads.

Once. It records ``continuous_hr_retimed`` in the connection's ``polar_pull_state`` and does
nothing while that is there. A Postgres advisory lock keeps two replicas starting together from
both running. Re-running it after a lost marker is safe: it replaces the same rows again.

Stops before changing anything, and reports, when a Polar sleep record stored in the window has
no night in Polar's listing: its own samples would be deleted and not stored again.

Does nothing at all unless ``POLAR_HR_RETIME_USER_ID`` is set, and set it only on dev: no
production row is touched by this item. On an environment where the id names no user with an
active Polar connection it does nothing either. Prints counts, never a value; ``uncorrected``
is P3's count of samples stored with no offset within three days.

Wired into ``scripts/start/app.sh``; prints and exits 0 when there is nothing to do.

Usage (inside the app container):
    POLAR_HR_RETIME_USER_ID=<uuid> uv run python scripts/data_migrations/retime_polar_continuous_hr.py
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import DataSource, EventRecord, UserConnection
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.enums.series_types import get_series_type_id
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.providers.polar.data_247 import _RANGE_WINDOW_DAYS, _SLEEP_LISTING, Polar247Data
from app.services.providers.polar.strategy import PolarStrategy
from app.services.timeseries_service import timeseries_service

USER_ID_VARIABLE = "POLAR_HR_RETIME_USER_ID"
MARKER = "continuous_hr_retimed"
_LOCK_KEY = "polar_continuous_hr_retime_2_96_8"

_DELETE = text("""
    DELETE FROM data_point_series dps
    USING data_source ds
    WHERE dps.data_source_id = ds.id
      AND ds.user_id = :user_id
      AND ds.provider = 'polar'
      AND dps.series_type_definition_id = :heart_rate
      AND dps.recorded_at >= :window_start
""")

_MARK = text("""
    INSERT INTO polar_pull_state (user_connection_id, full_fetched_at)
    VALUES (:connection_id, jsonb_build_object(CAST(:marker AS text), CAST(:now AS text)))
    ON CONFLICT (user_connection_id)
    DO UPDATE SET full_fetched_at = polar_pull_state.full_fetched_at || EXCLUDED.full_fetched_at
""")


@dataclass
class RetimeReport:
    outcome: str
    deleted: int = 0
    continuous: int = 0
    sleep: int = 0
    uncorrected: int = 0
    unlisted_nights: int = 0

    @property
    def stopped(self) -> bool:
        return self.outcome == "stopped"


def _say(report: RetimeReport) -> RetimeReport:
    print(
        f"polar continuous heart rate retime: outcome={report.outcome} deleted={report.deleted} "
        f"continuous={report.continuous} sleep={report.sleep} uncorrected={report.uncorrected} "
        f"unlisted_nights={report.unlisted_nights}"
    )
    return report


def _connection(db: Session, user_id: UUID) -> UserConnection | None:
    return (
        db.query(UserConnection)
        .filter(
            UserConnection.user_id == user_id,
            UserConnection.provider == ProviderName.POLAR.value,
            UserConnection.status == ConnectionStatus.ACTIVE,
        )
        .one_or_none()
    )


def _marked(db: Session, connection_id: UUID) -> bool:
    row = db.execute(
        text("SELECT full_fetched_at ->> :marker IS NOT NULL FROM polar_pull_state WHERE user_connection_id = :id"),
        {"marker": MARKER, "id": connection_id},
    ).scalar_one_or_none()
    return bool(row)


def _stored_nights(db: Session, user_id: UUID, window_start: datetime) -> list[tuple[datetime, datetime]]:
    rows = (
        db.query(EventRecord.start_datetime, EventRecord.end_datetime)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            DataSource.provider == ProviderName.POLAR,
            EventRecord.category == "sleep",
            EventRecord.end_datetime >= window_start,
        )
        .all()
    )
    return [(r.start_datetime, r.end_datetime) for r in rows]


def _fetch(
    db: Session, data: Polar247Data, user_id: UUID, now: datetime, window_start: datetime, report: RetimeReport
) -> list[TimeSeriesSampleCreate] | None:
    """Every Polar call, before any write. None when a stored night has no listed night."""
    listing = data._make_api_request(db, user_id, _SLEEP_LISTING)
    listed, _ = data._readable_available_nights(listing, user_id)
    nights: dict[str, dict[str, Any]] = {}
    for start, end in _stored_nights(db, user_id, window_start):
        matches = [
            night
            for _, night in listed
            if night.get("start_time")
            and night.get("end_time")
            and datetime.fromisoformat(night["start_time"]) < end
            and datetime.fromisoformat(night["end_time"]) > start
        ]
        if not matches:
            report.unlisted_nights += 1
        nights.update({night["date"]: night for night in matches})
    if report.unlisted_nights:
        return None

    since = now - timedelta(days=_RANGE_WINDOW_DAYS["continuous_hr"])
    items = data.get_continuous_hr_data(db, user_id, since, now)
    offsets = data._continuous_hr_offsets(db, user_id, listing, items)
    continuous = data.normalize_continuous_hr(items, user_id, offsets)
    report.continuous = len(continuous)
    report.uncorrected = offsets.uncorrected

    sleep: list[TimeSeriesSampleCreate] = []
    for day in sorted(nights):
        raw = data._make_api_request(db, user_id, f"/v3/users/sleep/{day}")
        for _, _, _, hr in data.normalize_sleep([raw] if raw else [], user_id):
            sleep.extend(hr)
    report.sleep = len(sleep)
    return continuous + sleep


def retime_polar_continuous_hr(db: Session, user_id: UUID | None, data: Polar247Data | None = None) -> RetimeReport:
    """Replace the account's Polar heart-rate rows in the window, or change nothing."""
    if user_id is None:
        return _say(RetimeReport("not_configured"))
    connection = _connection(db, user_id)
    if connection is None:
        return _say(RetimeReport("no_active_polar_connection"))
    # A session-level lock on its own connection: a Polar token refresh commits the work session.
    with db.get_bind().engine.connect() as lock:
        if not lock.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": _LOCK_KEY}).scalar():
            return _say(RetimeReport("running_elsewhere"))
        try:
            # Read under the lock: a replica that held it may have just finished.
            db.expire_all()
            if _marked(db, connection.id):
                return _say(RetimeReport("already_done"))
            data = data or _polar_data()
            now = datetime.now(timezone.utc)
            since = now - timedelta(days=_RANGE_WINDOW_DAYS["continuous_hr"])
            window_start = datetime.combine(since.date(), time(), tzinfo=timezone.utc)
            report = RetimeReport("done")
            samples = _fetch(db, data, user_id, now, window_start, report)
            if samples is None:
                report.outcome = "stopped"
                print("STOPPED: nothing changed. A stored Polar night is not in Polar's listing.")
                return _say(report)
            deleted = cast(
                CursorResult[tuple[()]],
                db.execute(
                    _DELETE,
                    {
                        "user_id": user_id,
                        "heart_rate": get_series_type_id(SeriesType.heart_rate),
                        "window_start": window_start,
                    },
                ),
            )
            report.deleted = deleted.rowcount
            timeseries_service.crud.bulk_create(db, samples)
            db.execute(_MARK, {"connection_id": connection.id, "marker": MARKER, "now": now.isoformat()})
            db.commit()
            return _say(report)
        except Exception:
            db.rollback()
            raise
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": _LOCK_KEY})
            lock.commit()


def _polar_data() -> Polar247Data:
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)
    return data


def _configured_user_id() -> UUID | None:
    value = os.environ.get(USER_ID_VARIABLE, "").strip()
    return UUID(value) if value else None


def main() -> int:
    with SessionLocal() as db:
        report = retime_polar_continuous_hr(db, _configured_user_id())
    return 2 if report.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
