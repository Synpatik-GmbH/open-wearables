"""2.96.6.1 (spec cause 4, L1): the connections API says when Polar has not accepted the user."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import PolarRegistration
from app.schemas.auth import ConnectionStatus
from app.services.providers.polar.strategy import PolarStrategy
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.utils import api_key_headers


def _registration(db: Session, connection_id: object, *, registered: bool) -> None:
    db.add(
        PolarRegistration(
            user_connection_id=connection_id,
            registered=registered,
            attempts=1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def _pending_by_provider(client: TestClient, user_id: object) -> dict[str, bool]:
    headers = api_key_headers(ApiKeyFactory().plain_key)
    response = client.get(f"/api/v1/users/{user_id}/connections", headers=headers)
    assert response.status_code == 200
    return {c["provider"]: c["registration_pending"] for c in response.json()}


def test_a_polar_connection_whose_registration_failed_is_pending(client: TestClient, db: Session) -> None:
    user = UserFactory()
    polar = UserConnectionFactory(user=user, provider="polar", status=ConnectionStatus.ACTIVE)
    UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.ACTIVE)
    _registration(db, polar.id, registered=False)

    assert _pending_by_provider(client, user.id) == {"polar": True, "garmin": False}


def test_a_registered_polar_connection_is_not_pending(client: TestClient, db: Session) -> None:
    user = UserFactory()
    polar = UserConnectionFactory(user=user, provider="polar", status=ConnectionStatus.ACTIVE)
    _registration(db, polar.id, registered=True)

    assert _pending_by_provider(client, user.id) == {"polar": False}


def test_a_polar_connection_with_no_registration_row_is_pending_until_registration_succeeds(
    client: TestClient, db: Session
) -> None:
    # Codex 4270734351 (spec cause 4, AC6): no row means Polar has not been seen to accept the
    # user (a connection made before 2.96.6.1 whose registration may have failed silently), so it
    # is never reported connected until a registration succeeds. Its next pull attempts it.
    user = UserFactory()
    polar = UserConnectionFactory(
        user=user, provider="polar", status=ConnectionStatus.ACTIVE, provider_user_id="4711", access_token="access"
    )

    assert _pending_by_provider(client, user.id) == {"polar": True}

    with patch("app.services.providers.polar.oauth.httpx.post", return_value=MagicMock(status_code=200)):
        assert PolarStrategy().oauth.ensure_registered(db, polar) is None

    assert _pending_by_provider(client, user.id) == {"polar": False}
