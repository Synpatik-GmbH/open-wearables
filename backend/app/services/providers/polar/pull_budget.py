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
  not asked is still due, so the next pull asks for it.
- Spec cause 3, AC4 (v0.6): a full fetch that failed is asked for again after a gap that doubles
  from ``RETRY_FIRST_GAP`` up to ``RETRY_MAX_GAP`` (``record_full_failure``), until it succeeds;
  its window moves on, so a date in it is dropped once it is older than 90 days. With pulls at
  most an hour apart, ``RETRY_MAX_GAP`` keeps every failed type asked for at least once every 22
  hours, while a Polar failing on every endpoint costs a member 136 calls on the worst-case day
  (``test_a_day_on_which_polar_fails_every_endpoint_stays_inside_the_limit``).
  The short forms are not held back: the newest night is asked for on every pull.
- Spec R2: Polar is asked for last night when the member opens the app (``polar/last_night.py``),
  past the pull interval, at most once every ``ON_OPEN_INTERVAL`` and ``ON_OPEN_DAILY_CAP`` times a
  Berlin day (``claim_on_open``). Each costs at most three calls, so a day of them adds at most 36
  to a worst-case day of 146, inside the 200 that 50 members leave each (AC17, AC18).
- 2.96.8 (spec v1.1, P7, AC11): a continuous heart-rate webhook costs two calls, the day and the
  sleep listing its offsets come from. At most ``WEBHOOK_HR_DAILY_CAP`` of them call Polar in any
  24 hours (``claim_continuous_hr_webhook``), so they add at most 18: 146 + 36 + 18 = 200. Past
  the cap a webhook calls nothing and the day is stored by the next full fetch.

The state is one ``PolarPullState`` row. Writes commit at once: the claim has to be visible to
a pull starting in another worker, and a fetch already stored must not be forgotten when a
later type of the same pull fails. The webhook claims are a Redis key per member: the instants
of the last 24 hours' claims, no health value, expiring 24 hours after the last.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import case, literal, select, type_coerce, update
from sqlalchemy.dialects.postgresql import JSONB, insert

from app.database import DbSession
from app.integrations.redis_client import get_redis_client
from app.models import PolarPullState

PULL_INTERVAL = timedelta(minutes=55)
FULL_INTERVAL = timedelta(hours=22)
RETRY_FIRST_GAP = timedelta(hours=1)
RETRY_MAX_GAP = timedelta(hours=21)
ON_OPEN_INTERVAL = timedelta(seconds=30)
ON_OPEN_DAILY_CAP = 12
WEBHOOK_HR_DAILY_CAP = 9
_WEBHOOK_HR_WINDOW = timedelta(hours=24)
# Drop the claims older than the window, then claim one if fewer than the cap are left. One
# script, so two webhooks at once cannot both claim the last one; only a claim is recorded, so
# any 24 hours hold at most the cap. KEYS[1] the member's key; ARGV now, window, cap, claim id.
_CLAIM_WEBHOOK_LUA = """
redis.call("zremrangebyscore", KEYS[1], "-inf", tonumber(ARGV[1]) - tonumber(ARGV[2]))
if redis.call("zcard", KEYS[1]) >= tonumber(ARGV[3]) then
    return 0
end
redis.call("zadd", KEYS[1], ARGV[1], ARGV[4])
redis.call("expire", KEYS[1], ARGV[2])
return 1
"""

# Why an ask on open, or a continuous heart-rate webhook, was refused.
TOO_SOON = "too_soon"
DAILY_CAP = "daily_cap"
COUNT_UNAVAILABLE = "count_unavailable"

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
        # Leave the pull's session usable: the caller skips the pull and records why.
        db.rollback()
        raise
    return claimed


def full_due(db: DbSession, connection_id: UUID, now: datetime, data_types: Iterable[str]) -> frozenset[str]:
    """The data types among ``data_types`` that this pull fetches in full: those with no full fetch
    in the last ``FULL_INTERVAL``, unless a failed one's retry gap has not passed yet."""
    state = db.get(PolarPullState, connection_id)
    fetched = state.full_fetched_at if state is not None else {}
    retries = (state.full_retry if state is not None else None) or {}
    due = set()
    for data_type in data_types:
        if not _gap_passed(retries.get(data_type), now):
            continue
        try:
            last = datetime.fromisoformat(fetched[data_type])
        except (KeyError, TypeError, ValueError):
            # Never fetched in full, or a value this code cannot read: fetch it.
            due.add(data_type)
            continue
        if last <= now - FULL_INTERVAL:
            due.add(data_type)
    return frozenset(due)


def _gap_passed(retry: object, now: datetime) -> bool:
    """True when no retry gap holds the type back. A value this code cannot read holds nothing."""
    if not isinstance(retry, dict):
        return True
    try:
        return now >= datetime.fromisoformat(retry["next"])
    except (KeyError, TypeError, ValueError):
        return True


def record_full_fetch(db: DbSession, connection_id: UUID, data_type: str, now: datetime) -> None:
    """``data_type`` was fetched in full at ``now`` without failing."""
    stamp = type_coerce({data_type: now.isoformat()}, JSONB)
    # A row made here starts no pull clock: the pull that fetched already claimed one, or ran without.
    stmt = insert(PolarPullState).values(user_connection_id=connection_id, full_fetched_at=stamp)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PolarPullState.user_connection_id],
        set_={
            "full_fetched_at": PolarPullState.full_fetched_at.op("||")(stamp),
            # A success ends the type's retry gaps: the next failure starts again at the first.
            "full_retry": PolarPullState.full_retry.op("-")(literal(data_type)),
        },
    )
    db.execute(stmt)
    db.commit()


def record_full_failure(
    db: DbSession, connection_id: UUID, data_type: str, now: datetime, *, started: bool = False
) -> None:
    """``data_type`` failed in full at ``now``: it is not asked for again until its gap has passed.

    The gap is ``RETRY_FIRST_GAP`` after the first failure in a row and doubles with each one
    after, up to ``RETRY_MAX_GAP``. A connection's pulls are one at a time (``claim_pull``), so
    reading the count and writing the next one is not raced. ``started`` is
    ``record_full_start``'s.
    """
    state = db.get(PolarPullState, connection_id)
    previous = ((state.full_retry if state is not None else None) or {}).get(data_type)
    failures = previous.get("failures", 0) if isinstance(previous, dict) else 0
    if not isinstance(failures, int) or failures < 0:
        failures = 0
    gap = min(RETRY_FIRST_GAP * (2 ** min(failures, 16)), RETRY_MAX_GAP)
    value: dict[str, object] = {"failures": failures + 1, "next": (now + gap).isoformat()}
    if started:
        value["started"] = now.isoformat()
    entry = type_coerce({data_type: value}, JSONB)
    stmt = insert(PolarPullState).values(user_connection_id=connection_id, full_retry=entry)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PolarPullState.user_connection_id],
        set_={"full_retry": PolarPullState.full_retry.op("||")(entry)},
    )
    db.execute(stmt)
    db.commit()


def record_full_start(db: DbSession, connection_id: UUID, data_type: str, now: datetime) -> None:
    """A full fetch of ``data_type`` starts at ``now``. It counts as failed until it ends: the
    failure and its gap are recorded now, with a ``started`` marker. A success clears the entry
    (``record_full_fetch``); a failure the pull caught keeps the gap and drops the marker
    (``record_full_end``). A marker still there on a later pull is a fetch that never ended: its
    worker was killed (prod, 16-19 Oct 2026, out of memory), and without the gap recorded up front
    every hourly pull started it again."""
    record_full_failure(db, connection_id, data_type, now, started=True)


def record_full_end(db: DbSession, connection_id: UUID, data_type: str) -> None:
    """The full fetch of ``data_type`` ended, its failure already recorded: drop its ``started``
    marker, keeping the gap."""
    state = db.execute(
        select(PolarPullState.full_retry).where(PolarPullState.user_connection_id == connection_id)
    ).scalar_one_or_none()
    entry = (state or {}).get(data_type)
    if not isinstance(entry, dict) or "started" not in entry:
        return
    kept = type_coerce({data_type: {k: v for k, v in entry.items() if k != "started"}}, JSONB)
    db.execute(
        update(PolarPullState)
        .where(PolarPullState.user_connection_id == connection_id)
        .values(full_retry=PolarPullState.full_retry.op("||")(kept))
    )
    db.commit()


def interrupted_full_fetches(db: DbSession, connection_id: UUID) -> frozenset[str]:
    """The data types whose last full fetch started and never ended (see ``record_full_start``)."""
    state = db.execute(
        select(PolarPullState.full_retry).where(PolarPullState.user_connection_id == connection_id)
    ).scalar_one_or_none()
    return frozenset(t for t, e in (state or {}).items() if isinstance(e, dict) and "started" in e)


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


def claim_continuous_hr_webhook(user_id: UUID, now: datetime) -> str | None:
    """Record that a continuous heart-rate webhook calls Polar now (2.96.8, P7).

    Returns None when claimed, or why not: ``DAILY_CAP`` while ``WEBHOOK_HR_DAILY_CAP`` were claimed
    in the 24 hours before ``now``, ``COUNT_UNAVAILABLE`` when the claims cannot be read. A webhook
    that is not claimed calls nothing, so claims that cannot be read cost nothing either. The
    window rolls rather than resetting at midnight: a calendar day would let the 24 hours across
    one midnight hold twice the cap.
    """
    window = int(_WEBHOOK_HR_WINDOW.total_seconds())
    try:
        claimed = get_redis_client().eval(
            _CLAIM_WEBHOOK_LUA,
            1,
            f"polar_hr_webhook:{user_id}",
            now.timestamp(),
            window,
            WEBHOOK_HR_DAILY_CAP,
            uuid4().hex,
        )
    except Exception:
        return COUNT_UNAVAILABLE
    return None if int(claimed) == 1 else DAILY_CAP


__all__ = [
    "COUNT_UNAVAILABLE",
    "DAILY_CAP",
    "FULL_INTERVAL",
    "ON_OPEN_DAILY_CAP",
    "ON_OPEN_INTERVAL",
    "PULL_INTERVAL",
    "RATE_LIMITED",
    "RETRY_FIRST_GAP",
    "RETRY_MAX_GAP",
    "TOO_SOON",
    "WEBHOOK_HR_DAILY_CAP",
    "claim_continuous_hr_webhook",
    "claim_on_open",
    "claim_pull",
    "full_due",
    "interrupted_full_fetches",
    "record_full_end",
    "record_full_failure",
    "record_full_fetch",
    "record_full_start",
]
