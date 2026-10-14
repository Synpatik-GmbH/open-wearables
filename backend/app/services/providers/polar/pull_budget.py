"""FORK (2.96.6.1, spec R1, AC17): Calibra's Polar calls stay inside Polar's daily limit.

Polar allows a client 5000 + 100 x users calls a day, and 500 + 20 x users every 15 minutes.
With 50 members that is 200 calls per member per day. Before this module a member cost
several hundred: every hourly pull asked every endpoint for its whole served window, and the
adapter's morning force-sync (every 15 minutes until the day is complete) cost the same again.

Three rules, all read and written here, all keyed on the Polar connection:

- At most one pull per connection every ``PULL_INTERVAL``. A pull that would start sooner is
  skipped before it calls Polar. The pull at connect is exempt (``ignore_interval``).
- A data type with a full form (see ``Polar247Data``) is fetched in full once its last full fetch
  without failure is ``FULL_INTERVAL`` old, or when it never had one. Between those, a pull
  fetches its short form or nothing. ``FULL_INTERVAL`` plus one pull interval stays under 24
  hours, so data Polar serves late is stored within 24 hours of being served (AC2).
- A 429 ends the pull (``RATE_LIMITED``): every later call in it would be refused too. What was
  not fetched is still due, so the next pull asks for it again (spec cause 3).
- Spec R2: Polar is asked for last night when the member opens the app (``polar/last_night.py``),
  past the pull interval, at most once every ``ON_OPEN_INTERVAL`` and ``ON_OPEN_DAILY_CAP`` times a
  Berlin day (``claim_on_open``). Each costs at most three calls, so a day of them adds at most 36
  to a worst-case day of 146, inside the 200 that 50 members leave each (AC17, AC18).

The state is one ``PolarPullState`` row. Writes commit at once: the claim has to be visible to
a pull starting in another worker, and a fetch already stored must not be forgotten when a
later type of the same pull fails.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import case, literal, type_coerce
from sqlalchemy.dialects.postgresql import JSONB, insert

from app.database import DbSession
from app.models import PolarPullState

PULL_INTERVAL = timedelta(minutes=55)
FULL_INTERVAL = timedelta(hours=22)
ON_OPEN_INTERVAL = timedelta(seconds=30)
ON_OPEN_DAILY_CAP = 12

# Why an ask on open was refused.
TOO_SOON = "too_soon"
DAILY_CAP = "daily_cap"

_BERLIN = ZoneInfo("Europe/Berlin")

# What error_code() makes of the HTTPException a 429 raises.
RATE_LIMITED = "HTTPException_429"


def claim_pull(db: DbSession, connection_id: UUID, now: datetime, *, ignore_interval: bool = False) -> bool:
    """Record that a pull starts now. False, and nothing recorded, when the last one started
    less than ``PULL_INTERVAL`` before ``now`` and ``ignore_interval`` is not set.

    One statement, so two pulls starting together cannot both claim.
    """
    stmt = insert(PolarPullState).values(user_connection_id=connection_id, pull_started_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PolarPullState.user_connection_id],
        set_={"pull_started_at": now},
        where=None
        if ignore_interval
        else (PolarPullState.pull_started_at.is_(None)) | (PolarPullState.pull_started_at <= now - PULL_INTERVAL),
    )
    try:
        claimed = db.execute(stmt.returning(PolarPullState.user_connection_id)).first() is not None
        db.commit()
    except Exception:
        # Leave the pull's session usable: the caller lets the pull run when the claim fails.
        db.rollback()
        raise
    return claimed


def full_due(db: DbSession, connection_id: UUID, now: datetime, data_types: Iterable[str]) -> frozenset[str]:
    """The data types among ``data_types`` that this pull fetches in full."""
    state = db.get(PolarPullState, connection_id)
    fetched = state.full_fetched_at if state is not None else {}
    due = set()
    for data_type in data_types:
        try:
            last = datetime.fromisoformat(fetched[data_type])
        except (KeyError, TypeError, ValueError):
            # Never fetched in full, or a value this code cannot read: fetch it.
            due.add(data_type)
            continue
        if last <= now - FULL_INTERVAL:
            due.add(data_type)
    return frozenset(due)


def record_full_fetch(db: DbSession, connection_id: UUID, data_type: str, now: datetime) -> None:
    """``data_type`` was fetched in full at ``now`` without failing."""
    stamp = type_coerce({data_type: now.isoformat()}, JSONB)
    # A row made here starts no pull clock: the pull that fetched already claimed one, or ran without.
    stmt = insert(PolarPullState).values(user_connection_id=connection_id, full_fetched_at=stamp)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PolarPullState.user_connection_id],
        set_={"full_fetched_at": PolarPullState.full_fetched_at.op("||")(stamp)},
    )
    db.execute(stmt)
    db.commit()


def forget_full_fetch(db: DbSession, connection_id: UUID, data_type: str) -> None:
    """Make ``data_type`` due in full on the next pull: its short form failed (spec cause 3)."""
    db.query(PolarPullState).filter(PolarPullState.user_connection_id == connection_id).update(
        {PolarPullState.full_fetched_at: PolarPullState.full_fetched_at.op("-")(literal(data_type))},
        synchronize_session=False,
    )
    db.commit()


def claim_on_open(db: DbSession, connection_id: UUID, now: datetime) -> str | None:
    """Record that Polar is asked for last night now because the member opened the app.

    Returns None when claimed, or why not: ``TOO_SOON`` inside ``ON_OPEN_INTERVAL`` of the last
    ask, ``DAILY_CAP`` once ``ON_OPEN_DAILY_CAP`` asks were made on this Berlin day. Nothing is
    recorded on a refusal. One statement, so two opens at once cannot both claim the last ask.
    It reads and moves nothing of the pull interval.
    """
    day = now.astimezone(_BERLIN).date()
    stmt = insert(PolarPullState).values(
        user_connection_id=connection_id, on_open_at=now, on_open_day=day, on_open_count=1
    )
    same_day = PolarPullState.on_open_day == day
    stmt = stmt.on_conflict_do_update(
        index_elements=[PolarPullState.user_connection_id],
        set_={
            "on_open_at": now,
            "on_open_day": day,
            "on_open_count": case((same_day, PolarPullState.on_open_count + 1), else_=1),
        },
        where=(PolarPullState.on_open_at.is_(None))
        | (
            (PolarPullState.on_open_at <= now - ON_OPEN_INTERVAL)
            & (~same_day | PolarPullState.on_open_day.is_(None) | (PolarPullState.on_open_count < ON_OPEN_DAILY_CAP))
        ),
    )
    try:
        claimed = db.execute(stmt.returning(PolarPullState.user_connection_id)).first() is not None
        db.commit()
    except Exception:
        db.rollback()
        raise
    if claimed:
        return None
    # The refused update left the row as it was; read it fresh to say why.
    last = db.query(PolarPullState.on_open_at).filter(PolarPullState.user_connection_id == connection_id).scalar()
    if last is not None and last > now - ON_OPEN_INTERVAL:
        return TOO_SOON
    return DAILY_CAP


__all__ = [
    "DAILY_CAP",
    "FULL_INTERVAL",
    "ON_OPEN_DAILY_CAP",
    "ON_OPEN_INTERVAL",
    "PULL_INTERVAL",
    "RATE_LIMITED",
    "TOO_SOON",
    "claim_on_open",
    "claim_pull",
    "forget_full_fetch",
    "full_due",
    "record_full_fetch",
]
