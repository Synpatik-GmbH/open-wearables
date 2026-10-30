"""FORK (Calibra 2.71.3.3, design D-15; spec 2.71.3 v0.8 P16, AC 23): Garmin's tokens stay alive.

Garmin's refresh token lasts about 90 days and every refresh returns a new one. Garmin delivers by
push, so nothing else refreshes a row, and without this the one deregistration attempt (D-05) is
refused for any member connected longer than that.

Nothing here ends a connection, whatever Garmin answers. A 400 can mean a bad client secret as well
as a dead token, and treating it as an end would disconnect every Garmin member, and withdraw every
Garmin consent, on one wrong setting. It is logged and alerted, and the row stays due.

The sweep's read only says which rows to look at. A row can be refreshed, revoked or reconnected
before its turn, so each is read again under the lock, and the token presented is the one it holds
then: an old one would be refused and page on a healthy connection.

No transaction stays open across a call to Garmin, as in D-05: each read ends before the call, so
the row is written by one conditional update (INV-10) rather than held.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session

from app.integrations.redis_client import get_redis_client
from app.models import UserConnection
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import OAuthTokenResponse
from app.services.providers.factory import ProviderFactory
from app.services.providers.garmin.deregistration import REFRESH_REJECTED_STATUSES, post_refresh
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# calibra-ow-deploy scripts/alerts.sh matches this literal, and "outcome": "refresh_rejected" and
# "outcome": "failed", as strings.
ACTION = "garmin_token_keepalive"

# P16: a refresh token with this long or less left is refreshed. A month of daily runs to notice
# and fix a job that is failing, before the token is gone.
MARGIN = timedelta(days=30)

# Longer than a refresh's deadline and its write together.
LOCK_SECONDS = 60

connection_repository = UserConnectionRepository(UserConnection)


class KeepaliveOutcome(StrEnum):
    REFRESHED = "refreshed"
    LOCKED = "locked"
    SUPERSEDED = "superseded"
    REFRESH_REJECTED = "refresh_rejected"
    FAILED = "failed"


ALERTED_OUTCOMES = (KeepaliveOutcome.REFRESH_REJECTED, KeepaliveOutcome.FAILED)


@dataclass(frozen=True)
class DueRow:
    """What the sweep needs of a row, copied out so that no ORM object outlives the read."""

    id: UUID
    user_id: UUID


def _refresh(
    db: Session, oauth: BaseOAuthTemplate, row: DueRow, due_by: datetime
) -> tuple[KeepaliveOutcome, str | None]:
    refresh_token = connection_repository.get_garmin_refresh_token_if_due(db, row.id, due_by)
    db.commit()
    if refresh_token is None:
        return KeepaliveOutcome.SUPERSEDED, None

    response = post_refresh(oauth, refresh_token)
    if response.status_code in REFRESH_REJECTED_STATUSES:
        return KeepaliveOutcome.REFRESH_REJECTED, None
    if not response.is_success:
        return KeepaliveOutcome.FAILED, f"refresh_status_{response.status_code}"

    answer = OAuthTokenResponse.model_validate(response.json())
    if not answer.refresh_token:
        # The new token is the whole point, and the old one may already be dead.
        return KeepaliveOutcome.FAILED, "refresh_no_refresh_token"

    stored = connection_repository.store_refreshed_tokens(
        db,
        row.id,
        presented_refresh_token=refresh_token,
        access_token=answer.access_token,
        refresh_token=answer.refresh_token,
        expires_in=answer.expires_in,
        refresh_token_expires_in=answer.refresh_token_lifetime,
    )
    return (KeepaliveOutcome.REFRESHED if stored else KeepaliveOutcome.SUPERSEDED), None


def _keep_alive(
    db: Session, oauth: BaseOAuthTemplate, row: DueRow, due_by: datetime
) -> tuple[KeepaliveOutcome, str | None]:
    """The row's outcome, and for a failure with no answer to read, the exception's class name."""
    try:
        # The lock api_client.get_valid_token refreshes under, so the two never present one token.
        lock = get_redis_client().lock(
            f"token_refresh_lock:{ProviderName.GARMIN.value}:{row.user_id}", timeout=LOCK_SECONDS
        )
        if not lock.acquire(blocking=False):
            return KeepaliveOutcome.LOCKED, None
        try:
            return _refresh(db, oauth, row, due_by)
        finally:
            lock.release()
    except Exception as e:
        # The class name only, for the reason D-05 gives: the text can quote a URL or a body, and
        # Sentry would report frames that hold a refresh token. Rolled back so that a failed
        # statement does not poison the next row.
        db.rollback()
        return KeepaliveOutcome.FAILED, type(e).__name__


def keep_tokens_alive(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Refresh every due Garmin row, each on its own, and log one line for each."""
    due_by = (now or datetime.now(timezone.utc)) + MARGIN
    rows = [
        DueRow(connection.id, connection.user_id)
        for connection in connection_repository.get_garmin_refresh_tokens_due(db, due_by)
    ]
    db.commit()

    oauth = cast(BaseOAuthTemplate, ProviderFactory().get_provider(ProviderName.GARMIN.value).oauth)
    outcomes: Counter[str] = Counter()
    for row in rows:
        outcome, cause = _keep_alive(db, oauth, row, due_by)
        outcomes[outcome.value] += 1
        log_structured(
            logger,
            "error" if outcome in ALERTED_OUTCOMES else "info",
            "Garmin token keep-alive",
            provider=ProviderName.GARMIN.value,
            action=ACTION,
            outcome=outcome.value,
            user_id=str(row.user_id),
            **({"cause": cause} if cause else {}),
        )
    return dict(outcomes)
