"""Garmin never backfills on connect (Calibra 2.71.3.3, design D-09 and INV-06; spec 2.1)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas.auth import ConnectionStatus
from tests.factories import UserConnectionFactory, UserFactory


def callback(client: TestClient, db: Session, provider: str, *, webhook_callback: bool, scope: str) -> None:
    strategy = MagicMock()
    strategy.capabilities.webhook_callback = webhook_callback
    strategy.capabilities.rest_pull = not webhook_callback
    member = UserFactory()
    db.add(
        UserConnectionFactory.build(user_id=member.id, provider=provider, status=ConnectionStatus.ACTIVE, scope=scope)
    )
    db.flush()
    strategy.oauth.handle_callback.return_value = MagicMock(user_id=member.id, redirect_uri=None)

    with (
        patch("app.api.routes.v1.oauth.get_oauth_strategy", return_value=strategy),
        patch("app.api.routes.v1.oauth.user_connection_service.stamp_last_synced_at"),
    ):
        response = client.get(
            f"/api/v1/oauth/{provider}/callback", params={"code": "c", "state": "s"}, follow_redirects=False
        )
    assert response.status_code == 303


@pytest.mark.parametrize("granted", ["HEALTH_EXPORT", "HEALTH_EXPORT HISTORICAL_DATA_EXPORT"])
def test_garmin_queues_no_backfill_whatever_the_member_granted(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch, granted: str
) -> None:
    monkeypatch.setattr(settings, "historical_sync_on_connect", True)

    with (
        patch("app.integrations.celery.tasks.start_garmin_full_backfill") as backfill,
        patch("app.integrations.celery.tasks.sync_vendor_data") as pull,
    ):
        callback(client, db, "garmin", webhook_callback=True, scope=granted)

    backfill.delay.assert_not_called()
    pull.delay.assert_not_called()


def test_another_provider_keeps_the_setting(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "historical_sync_on_connect", True)

    with patch("app.integrations.celery.tasks.sync_vendor_data") as pull:
        callback(client, db, "polar", webhook_callback=False, scope="read")

    pull.delay.assert_called_once()
