"""FORK (2.96.6.1, spec R2, AC18): ask Polar for a member's last night because they opened the app.

Calibra calls this, through ``POST /api/v1/providers/polar/users/{user_id}/sync/last-night``, when
a Polar member opens the app and last night is not stored. It runs in the request and answers once
what Polar served is stored, so Calibra can score the night before the app reads the day.

It is not a pull: the 55-minute pull interval does not hold it back and it does not move it. It has
its own limits instead (``pull_budget.claim_on_open``): one ask every 30 seconds, twelve a Berlin
day. An ask is at most three calls: the nights Polar lists, the newest of them when it is not
stored, and Nightly Recharge, which is where a Polar night's HRV comes from. Like every Polar call
here, a 429 is not retried.

The answer is ``{"outcome", "reason", "stored"}``:

- ``fetched``: Polar was asked; ``stored`` counts the night and the Recharge results stored.
- ``refused``: Polar was not asked; ``reason`` is ``not_connected``, ``registration_pending``,
  ``too_soon`` or ``daily_cap``.
- ``failed``: Polar was asked and a call failed; ``reason`` is its code. What was stored before the
  failure is counted.
"""

from datetime import datetime, timezone
from typing import Any, TypedDict
from uuid import UUID

from app.database import DbSession
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName
from app.schemas.sync_status import DataTypeFailure
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.oauth import polar_registration_pending
from app.services.providers.polar.pull_budget import claim_on_open
from app.services.sync_error_code import UNKNOWN, error_code
from app.utils.structured_logging import log_structured

FETCHED = "fetched"
REFUSED = "refused"
FAILED = "failed"
NOT_CONNECTED = "not_connected"
REGISTRATION_PENDING = "registration_pending"


class LastNightAnswer(TypedDict):
    outcome: str
    reason: str | None
    stored: int


def _answer(outcome: str, reason: str | None = None, stored: int = 0) -> LastNightAnswer:
    return {"outcome": outcome, "reason": reason, "stored": stored}


def ask_polar_for_last_night(db: DbSession, data_247: Polar247Data, user_id: UUID, logger: Any) -> LastNightAnswer:
    """Ask Polar for the member's newest night and Nightly Recharge, and store them."""
    connection = UserConnectionRepository().get_active_connection(db, user_id, ProviderName.POLAR.value)
    if connection is None:
        return _answer(REFUSED, NOT_CONNECTED)
    # Polar serves nothing for a user it has not accepted (spec cause 4).
    if polar_registration_pending(db, connection.id):
        return _answer(REFUSED, REGISTRATION_PENDING)

    now = datetime.now(timezone.utc)
    if (refused := claim_on_open(db, connection.id, now)) is not None:
        return _answer(REFUSED, refused)

    stored = 0
    failure: str | None = None

    # The night list raises; a night that fails comes back as a DataTypeFailure.
    try:
        nights = data_247._save_missing_sleep(db, user_id, now, latest_only=True)
    except Exception as e:
        db.rollback()
        nights = DataTypeFailure(code=error_code(e) or UNKNOWN)
    if isinstance(nights, DataTypeFailure):
        stored += nights.saved
        failure = nights.code
    else:
        stored += nights
        db.commit()

    if failure is None:
        try:
            recharges = data_247.get_nightly_recharge_data(db, user_id, now, now)
            stored += data_247._save_nightly_recharge(db, *data_247.normalize_nightly_recharge(recharges, user_id))
            db.commit()
        except Exception as e:
            db.rollback()
            failure = error_code(e) or UNKNOWN

    # A code, never the exception's text (spec cause 3).
    log_structured(
        logger,
        "info",
        "Polar asked for last night on open",
        provider="polar",
        task="ask_polar_for_last_night",
        user_id=str(user_id),
        stored=stored,
        error_code=failure,
    )
    if failure is not None:
        return _answer(FAILED, failure, stored)
    return _answer(FETCHED, None, stored)
