from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.services import DeveloperDep

router = APIRouter()


class ConfigResponse(BaseModel):
    """Instance feature flags for the admin panel. Additive-only: append new flags,
    never remove or repurpose, so the frontend stays backward compatible."""

    outgoing_webhooks_enabled: bool
    # FORK (2.71.4): the dashboard's "Connect Mobile App" action needs these routes.
    user_invitation_codes_enabled: bool


@router.get("/config", response_model=ConfigResponse)
def get_config(_developer: DeveloperDep):
    return ConfigResponse(
        outgoing_webhooks_enabled=settings.outgoing_webhooks_enabled,
        user_invitation_codes_enabled=settings.user_invitation_codes_enabled,
    )
