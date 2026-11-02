"""The providers whose one attempt at the provider follows the revoke (Calibra 2.72.3, design D-07).

FORK: a disconnect and an account deletion read these providers' tokens into memory, end the
connection, and only then make one attempt with what they read. Every other provider keeps the
best-effort ``deregister_user`` before the revoke. Garmin's entry calls Garmin's attempt with the
arguments it has always had (2.71.3.3 D-05, D-06).
"""

from collections.abc import Callable
from uuid import UUID

from app.schemas.enums import ProviderName
from app.services.providers.garmin.deregistration import DeregistrationTrigger, HeldTokens, attempt_deregistration
from app.services.providers.google_health.revocation import attempt_revoke
from app.services.providers.templates.base_oauth import BaseOAuthTemplate

Attempt = Callable[[BaseOAuthTemplate, HeldTokens, DeregistrationTrigger, UUID], object]

ATTEMPT_AFTER_REVOKE: dict[str, Attempt] = {
    ProviderName.GARMIN.value: lambda oauth, held, trigger, user_id: attempt_deregistration(
        oauth, held, trigger=trigger, user_id=user_id
    ),
    ProviderName.GOOGLE_HEALTH.value: lambda _oauth, held, trigger, user_id: attempt_revoke(
        held, trigger=trigger, user_id=user_id
    ),
}


def attempts_after_revoke(provider: str) -> bool:
    return provider in ATTEMPT_AFTER_REVOKE


def attempt_after_revoke(
    provider: str, oauth: BaseOAuthTemplate, held: HeldTokens, *, trigger: DeregistrationTrigger, user_id: UUID
) -> None:
    """Make the provider's one attempt. Each attempt logs its own line and never raises."""
    ATTEMPT_AFTER_REVOKE[provider](oauth, held, trigger, user_id)
