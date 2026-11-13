"""One deregistration attempt at Polar AccessLink (Calibra GL-20; the Polar rule R-2026-10-26-c).

FORK: the rule Garmin (2.71.3.3 D-05) and Google Health (2.72.3 D-07) were built to, now kept by
Polar itself. The connection ends in Calibra first, whatever Polar does; then one
``DELETE /v3/users/{polar-user-id}`` with the access token read before the row lost it. The outcome
is logged once, ``failed`` alerts, and nothing waits on it or retries.

Polar needs no refresh first: AccessLink issues no refresh token, and its access token is
long-lived. The user id is Polar's, from the connection row; without it there is nothing to call.

The deadline and the request helper are this module's own, in the shape of Garmin's
(``garmin/deregistration.py``), for the reason Google's are: each provider's tests patch its own.
"""

import asyncio
from enum import StrEnum
from logging import getLogger
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from app.services.providers.garmin.deregistration import DeregistrationTrigger, HeldTokens
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

# calibra-ow-deploy scripts/alerts.sh matches this literal, and "outcome": "failed", as strings.
ACTION = "polar_deregistration"

# One total deadline, connect to last byte, inside the adapter's 15-second timeout for a disconnect.
DEADLINE_SECONDS = 5.0


class PolarDeregistrationOutcome(StrEnum):
    CONFIRMED = "confirmed"
    NOT_FOUND = "not_found"
    NO_CREDENTIAL = "no_credential"
    FAILED = "failed"


# ``not_found`` does not alert: Polar holds no registration for the user, which is the state the
# attempt exists to reach.
ALERTED_OUTCOMES = (PolarDeregistrationOutcome.FAILED,)


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    async def call() -> httpx.Response:
        async with httpx.AsyncClient(timeout=DEADLINE_SECONDS) as client:
            return await client.request(method, url, **kwargs)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(call(), DEADLINE_SECONDS))
    finally:
        loop.close()


def _attempt(oauth: BaseOAuthTemplate, held: HeldTokens) -> tuple[PolarDeregistrationOutcome, str | None]:
    """The outcome, and for a failure, the status or the exception's class name."""
    if not held.access_token or not held.provider_user_id:
        return PolarDeregistrationOutcome.NO_CREDENTIAL, None

    try:
        response = _request(
            "DELETE",
            f"{oauth.api_base_url}/v3/users/{quote(held.provider_user_id, safe='')}",
            headers={"Authorization": f"Bearer {held.access_token}"},
        )
    except Exception as e:
        # A timeout, a transport error. The class name says which: the text can quote a URL or a
        # body, and these frames hold a token, so nothing here goes to Sentry either.
        return PolarDeregistrationOutcome.FAILED, type(e).__name__

    if response.is_success:
        return PolarDeregistrationOutcome.CONFIRMED, None
    if response.status_code == 404:
        return PolarDeregistrationOutcome.NOT_FOUND, None
    return PolarDeregistrationOutcome.FAILED, str(response.status_code)


def attempt_deregistration(
    oauth: BaseOAuthTemplate,
    held: HeldTokens,
    *,
    trigger: DeregistrationTrigger,
    user_id: UUID,
) -> PolarDeregistrationOutcome:
    """Make the one attempt and log its one line. Never raises."""
    try:
        outcome, cause = _attempt(oauth, held)
    except Exception as e:
        outcome, cause = PolarDeregistrationOutcome.FAILED, type(e).__name__
    log_structured(
        logger,
        "error" if outcome in ALERTED_OUTCOMES else "info",
        "Polar deregistration attempt",
        provider="polar",
        action=ACTION,
        outcome=outcome.value,
        trigger=trigger.value,
        user_id=str(user_id),
        **({"cause": cause} if outcome is PolarDeregistrationOutcome.FAILED else {}),
    )
    return outcome
