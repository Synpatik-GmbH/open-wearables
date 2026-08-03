"""Concurrent-refresh safety for rotating refresh tokens.

Providers that ROTATE refresh tokens (Whoop, and others) invalidate the old
token the moment a refresh succeeds. Two workers handling two webhooks for the
same user therefore race: both load the connection, both hold the same refresh
token, the first refresh wins and rotates it, and the loser's request is
rejected with 400 — even though the connection is perfectly healthy and a fresh
access token was just written.

Before this fix ``refresh_access_token`` read that 400 as "the refresh token is
dead" and revoked the connection, which silently stopped all ingestion for the
user until they reconnected by hand. Observed on Whoop in dev on 2026-07-27 and
2026-08-02, both times at ~04:31 UTC when ``sleep.updated`` and
``recovery.updated`` arrived together and were picked up by the default and
fast-lane workers respectively.

The rule these tests pin: a 400/401 revokes ONLY when the stored refresh token
is still the one we presented. If it changed underneath us, another worker
rotated it and the connection must survive.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import User
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import ConnectionStatus
from app.services.providers.whoop.oauth import WhoopOAuth
from tests.factories import UserConnectionFactory, UserFactory


def _rejecting_post(status_code: int = 400) -> MagicMock:
    """A httpx.post that fails the way Whoop fails a spent refresh token."""
    response = httpx.Response(
        status_code=status_code,
        json={
            "error": "invalid_request",
            "error_description": "The request is missing a required parameter, includes an "
            "invalid parameter value, includes a parameter more than once, or is "
            "otherwise malformed",
        },
        request=httpx.Request("POST", "https://api.prod.whoop.com/oauth/oauth2/token"),
    )
    return MagicMock(side_effect=httpx.HTTPStatusError("400", request=response.request, response=response))


def _whoop_oauth(db: Session) -> WhoopOAuth:
    return WhoopOAuth(
        user_repo=UserRepository(User),
        connection_repo=UserConnectionRepository(),
        provider_name="whoop",
        api_base_url="https://api.prod.whoop.com/developer",
    )


class TestConcurrentRefreshRace:
    def test_does_not_revoke_when_another_worker_already_rotated_the_token(
        self,
        db: Session,
    ) -> None:
        """The loser of the race must leave the healthy connection alone."""
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user,
            provider="whoop",
            status=ConnectionStatus.ACTIVE,
            access_token="access-NEW",
            refresh_token="refresh-NEW",  # the winner already wrote this
            token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.commit()

        oauth = _whoop_oauth(db)

        # We present the token we loaded BEFORE the winner rotated it.
        with (
            patch("httpx.post", _rejecting_post()),
            patch("app.services.providers.templates.base_oauth.on_connection_revoked"),
        ):
            result = oauth.refresh_access_token(db, user.id, "refresh-OLD")

        db.refresh(connection)
        assert connection.status == ConnectionStatus.ACTIVE, (
            "a concurrent rotation is not a revocation — the connection is healthy"
        )
        assert result.access_token == "access-NEW", (
            "the caller should get the token the winning worker just stored, so the "
            "webhook it is processing still succeeds"
        )

    def test_revokes_when_the_stored_token_is_the_one_that_was_rejected(
        self,
        db: Session,
    ) -> None:
        """A genuinely dead refresh token must still revoke — no regression."""
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user,
            provider="whoop",
            status=ConnectionStatus.ACTIVE,
            access_token="access-1",
            refresh_token="refresh-1",  # unchanged: nobody else refreshed
            token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        db.commit()

        oauth = _whoop_oauth(db)

        with (
            patch("httpx.post", _rejecting_post()),
            patch("app.services.providers.templates.base_oauth.on_connection_revoked"),
            pytest.raises(HTTPException) as exc,
        ):
            oauth.refresh_access_token(db, user.id, "refresh-1")

        assert exc.value.status_code == 401
        db.refresh(connection)
        assert connection.status == ConnectionStatus.REVOKED, "the user really did revoke us — reconnection is required"
