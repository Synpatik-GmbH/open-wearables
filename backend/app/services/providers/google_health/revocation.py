"""One revoke attempt at Google (Calibra 2.72.3, design D-07; spec P12 and AC 17).

FORK: the Polar and Garmin rule (R-2026-10-26-c) for Google Health. The connection ends in Calibra
first, whatever Google does; then one ``POST`` to Google's revoke endpoint with a token read before
the row lost it. The outcome is logged once, ``failed`` alerts, and nothing waits on it or retries.

Google needs no refresh first: its revoke takes a refresh token or an access token, and revoking an
access token also revokes its refresh token (Google Identity, "Using OAuth 2.0 for Web Server
Applications", Revoking a token). The body carries the token only, no client credential, so a wrong
client secret cannot fail it.

The deadline and the request helper are this module's own, in the shape of Garmin's
(``garmin/deregistration.py``). They are not shared: Garmin's tests patch Garmin's constant, and a
shared one would leave those patches reaching nothing.
"""

import asyncio
import json
from enum import StrEnum
from logging import getLogger
from typing import Any
from uuid import UUID

import httpx

from app.services.providers.garmin.deregistration import DeregistrationTrigger, HeldTokens
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

# calibra-ow-deploy scripts/alerts.sh matches this literal, and "outcome": "failed", as strings.
ACTION = "google_health_revoke"

REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# One total deadline, connect to last byte, inside the adapter's 15-second timeout for Open Wearables.
DEADLINE_SECONDS = 5.0


class RevokeOutcome(StrEnum):
    CONFIRMED = "confirmed"
    ALREADY_INVALID = "already_invalid"
    NO_CREDENTIAL = "no_credential"
    FAILED = "failed"


# ``already_invalid`` does not alert: the grant is already gone at Google, which is the state the
# attempt exists to reach.
ALERTED_OUTCOMES = (RevokeOutcome.FAILED,)


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    async def call() -> httpx.Response:
        async with httpx.AsyncClient(timeout=DEADLINE_SECONDS) as client:
            return await client.request(method, url, **kwargs)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(call(), DEADLINE_SECONDS))
    finally:
        loop.close()


def _is_invalid_token(response: httpx.Response) -> bool:
    try:
        answer = json.loads(response.content)
    except ValueError:
        return False
    return isinstance(answer, dict) and answer.get("error") == "invalid_token"


def _attempt(held: HeldTokens) -> tuple[RevokeOutcome, str | None]:
    """The outcome, and for a failure, the status or the exception's class name."""
    token = held.refresh_token or held.access_token
    if not token:
        return RevokeOutcome.NO_CREDENTIAL, None

    try:
        response = _request("POST", REVOKE_URL, data={"token": token})
    except Exception as e:
        # A timeout, a transport error. The class name says which: the text can quote a URL or a
        # body, and these frames hold a token, so nothing here goes to Sentry either.
        return RevokeOutcome.FAILED, type(e).__name__

    if response.status_code == 200:
        return RevokeOutcome.CONFIRMED, None
    if response.status_code == 400 and _is_invalid_token(response):
        return RevokeOutcome.ALREADY_INVALID, None
    return RevokeOutcome.FAILED, str(response.status_code)


def attempt_revoke(held: HeldTokens, *, trigger: DeregistrationTrigger, user_id: UUID) -> RevokeOutcome:
    """Make the one attempt and log its one line. Never raises."""
    try:
        outcome, cause = _attempt(held)
    except Exception as e:
        outcome, cause = RevokeOutcome.FAILED, type(e).__name__
    log_structured(
        logger,
        "error" if outcome in ALERTED_OUTCOMES else "info",
        "Google Health revoke attempt",
        provider="google_health",
        action=ACTION,
        outcome=outcome.value,
        trigger=trigger.value,
        user_id=str(user_id),
        **({"cause": cause} if outcome is RevokeOutcome.FAILED else {}),
    )
    return outcome
