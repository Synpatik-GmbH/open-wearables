from datetime import datetime, timezone
from uuid import UUID

import httpx

from app.config import settings
from app.database import DbSession
from app.models import PolarRegistration, UserConnection
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthState,
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.sync_error_code import POLAR_USER_ID_MISSING, UNKNOWN, error_code


class PolarOAuth(BaseOAuthTemplate):
    """Polar OAuth 2.0 implementation."""

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url="https://flow.polar.com/oauth2/authorization",
            token_url="https://polarremote.com/v2/oauth2/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.polar_client_id or "",
            client_secret=(settings.polar_client_secret.get_secret_value() if settings.polar_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.POLAR),
            default_scope=settings.polar_default_scope,
        )

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Extracts the Polar user ID from the token response.

        FORK (2.96.6.1): registration moved to ``_save_connection``, after the connection exists,
        so its outcome can be recorded against it.
        """
        raw = token_response.model_extra.get("x_user_id") if token_response.model_extra else None
        provider_user_id = str(raw) if raw is not None else None
        return {"user_id": provider_user_id, "username": None}

    def _register_user(self, access_token: str, member_id: str) -> str | None:
        """Register the user with Polar. None when Polar accepted it, otherwise a failure code.

        FORK (2.96.6.1, spec cause 4): 2xx is registered and 409 is "already registered", which
        is registered too. Anything else, or no answer, is a failure with a code from
        ``app.services.sync_error_code``, never text. It used to swallow every outcome, including
        a 4xx or 5xx answer, and the connection read as connected while Polar served it nothing.
        """
        register_url = f"{self.api_base_url}/v3/users"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = httpx.post(register_url, json={"member-id": member_id}, headers=headers, timeout=10.0)
        except Exception as exc:
            return error_code(exc) or UNKNOWN
        if 200 <= response.status_code < 300 or response.status_code == 409:
            return None
        return error_code(f"HTTPStatusError_{response.status_code}") or UNKNOWN

    def _save_connection(
        self,
        db: DbSession,
        user_id: UUID,
        token_response: OAuthTokenResponse,
        user_info: dict[str, str | None],
        oauth_state: OAuthState,
    ) -> None:
        """Store the connection, register the user with Polar, and announce it only once registered.

        FORK (2.96.6.1, spec cause 4, L1): ``connection.created`` is what tells Calibra a member
        connected (it writes the connection record from it, AC7). Announcing a connection Polar
        has not accepted would record a source that never delivers, so a failed registration is
        recorded as pending, retried at the start of every pull, and announced when it succeeds.
        """
        connection, announce, connected_at = self._store_connection(db, user_id, token_response, user_info)
        was_pending = polar_registration_pending(db, connection.id)
        if user_info.get("user_id"):
            failure = self._register_user(token_response.access_token, str(user_id))
        else:
            failure = POLAR_USER_ID_MISSING
        record_polar_registration(db, connection.id, failure)
        if failure is None and (announce or was_pending):
            self._announce_connection(user_id, connection.id, connected_at)

    def ensure_registered(self, db: DbSession, connection: UserConnection) -> str | None:
        """Retry a registration that has not succeeded. None when registered, else a failure code.

        FORK (2.96.6.1, spec cause 4, AC6). Called at the start of every pull. A connection with
        no registration row predates 2.96.6.1: it is attempted once like a pending one, so a
        registration that failed silently before the fix is retried as well, but it is not
        announced again when it succeeds (it was announced when it was made).
        """
        row = db.get(PolarRegistration, connection.id)
        if row is not None and row.registered:
            return None
        if connection.provider_user_id and connection.access_token:
            failure = self._register_user(connection.access_token, str(connection.user_id))
        else:
            failure = POLAR_USER_ID_MISSING
        record_polar_registration(db, connection.id, failure)
        if failure is None and row is not None:
            self._announce_connection(connection.user_id, connection.id, datetime.now(timezone.utc).isoformat())
        return failure

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Call Polar's user deregistration endpoint to remove the app association."""

        if not provider_user_id:
            raise ValueError("Polar deregistration requires provider_user_id")

        deregister_url = f"{self.api_base_url}/v3/users/{provider_user_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
        }
        response = httpx.delete(deregister_url, headers=headers, timeout=10.0)
        response.raise_for_status()


def polar_registration_pending(db: DbSession, connection_id: UUID) -> bool:
    """True when the last registration attempt for this connection failed."""
    row = db.get(PolarRegistration, connection_id)
    return row is not None and not row.registered


def record_polar_registration(db: DbSession, connection_id: UUID, failure: str | None) -> PolarRegistration:
    """Record the outcome of one registration attempt. Commits."""
    row = db.get(PolarRegistration, connection_id)
    now = datetime.now(timezone.utc)
    if row is None:
        row = PolarRegistration(user_connection_id=connection_id, registered=False, attempts=0, updated_at=now)
        db.add(row)
    row.registered = failure is None
    row.attempts = (row.attempts or 0) + 1
    row.last_error_code = failure
    row.updated_at = now
    db.commit()
    return row
