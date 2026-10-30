"""FORK (Calibra 2.71.3.3, design D-05; spec 2.71.3 v0.7 P15): one deregistration attempt at Garmin.

The connection ends on our side first; this runs afterwards, from tokens held in memory, and
nothing waits on what it finds. Same rule as Polar (ruling R-2026-10-26-c): one attempt, the
outcome logged, a failure alerted, no retry.

The access token on the row can sit expired until disconnect: Garmin delivers by push, so no
outbound call refreshes it. Hence the refresh comes first, always.

The refresh here is not ``BaseOAuthTemplate.refresh_access_token``. That one writes the new tokens
to the row and revokes it on a rejection; here the row is already revoked and holds nothing, and
this function has no database session to write with.

``asyncio`` is what makes the deadline total. ``httpx`` timeouts are per phase, so a server sending
one byte each second never trips them; cancelling the request is the only bound that reaches the
last byte, and it closes the socket. Every caller is synchronous (a threadpool route or a Celery
task), so ``asyncio.run`` has no running loop to collide with.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

import httpx

from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# calibra-ow-deploy scripts/alerts.sh matches this literal, and "outcome": "failed", as strings.
ACTION = "garmin_deregistration"

# One total deadline per call, connect to last byte. Two calls at most, so an attempt spends ten
# seconds of Garmin time, inside the adapter's 15-second timeout for Open Wearables.
DEADLINE_SECONDS = 5.0

# The statuses refresh_access_token treats as a rejection.
REFRESH_REJECTED_STATUSES = (400, 401)


class DeregistrationOutcome(StrEnum):
    CONFIRMED = "confirmed"
    NOT_FOUND = "not_found"
    REFRESH_REJECTED = "refresh_rejected"
    NO_CREDENTIAL = "no_credential"
    FAILED = "failed"


class DeregistrationTrigger(StrEnum):
    DISCONNECT = "disconnect"
    PERMISSION_CHANGE = "permission_change"
    USER_DELETE = "user_delete"


@dataclass(frozen=True)
class HeldTokens:
    """A connection's tokens, read before the row lost them. Never printed."""

    access_token: str | None = field(repr=False)
    refresh_token: str | None = field(repr=False)

    @property
    def any_held(self) -> bool:
        return bool(self.access_token) or bool(self.refresh_token)


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    async def call() -> httpx.Response:
        async with httpx.AsyncClient(timeout=DEADLINE_SECONDS) as client:
            return await client.request(method, url, **kwargs)

    async def bounded() -> httpx.Response:
        return await asyncio.wait_for(call(), DEADLINE_SECONDS)

    return asyncio.run(bounded())


def _attempt(oauth: BaseOAuthTemplate, held: HeldTokens) -> DeregistrationOutcome:
    if not held.refresh_token:
        return DeregistrationOutcome.NO_CREDENTIAL

    try:
        data, headers = oauth._prepare_refresh_request(held.refresh_token)
        refresh = _request("POST", oauth.endpoints.token_url, data=data, headers=headers)
        if refresh.status_code in REFRESH_REJECTED_STATUSES:
            return DeregistrationOutcome.REFRESH_REJECTED
        if not refresh.is_success:
            return DeregistrationOutcome.FAILED
        answer = refresh.json()
        access_token = answer.get("access_token") if isinstance(answer, dict) else None
        if not access_token or not isinstance(access_token, str):
            return DeregistrationOutcome.FAILED

        deregistration = _request(
            "DELETE",
            f"{oauth.api_base_url}/partner-gateway/rest/user/registration",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except Exception:
        # A timeout, a transport error, an unreadable answer. Exception text can quote a URL or a
        # body, so none of it is logged.
        return DeregistrationOutcome.FAILED

    if deregistration.is_success:
        return DeregistrationOutcome.CONFIRMED
    if deregistration.status_code == 404:
        return DeregistrationOutcome.NOT_FOUND
    return DeregistrationOutcome.FAILED


def attempt_deregistration(
    oauth: BaseOAuthTemplate,
    held: HeldTokens,
    *,
    trigger: DeregistrationTrigger,
    user_id: UUID,
) -> DeregistrationOutcome:
    """Make the one attempt and log its one line. Never raises."""
    outcome = _attempt(oauth, held)
    log_structured(
        logger,
        "error" if outcome is DeregistrationOutcome.FAILED else "info",
        "Garmin deregistration attempt",
        provider="garmin",
        action=ACTION,
        outcome=outcome.value,
        trigger=trigger.value,
        user_id=str(user_id),
    )
    return outcome
