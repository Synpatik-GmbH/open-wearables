"""2.96.6.1 (spec R1, AC1): the pull queued at connect is not held back by the pull interval."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factories import UserConnectionFactory, UserFactory


def test_the_pull_queued_at_connect_ignores_the_pull_interval(client: TestClient, db: Session) -> None:
    strategy = MagicMock()
    strategy.capabilities.webhook_callback = False
    strategy.capabilities.rest_pull = True
    member = UserFactory()
    db.add(UserConnectionFactory.build(user_id=member.id, provider="polar"))
    db.flush()
    strategy.oauth.handle_callback.return_value = MagicMock(user_id=member.id, redirect_uri=None)

    with (
        patch("app.api.routes.v1.oauth.get_oauth_strategy", return_value=strategy),
        patch("app.api.routes.v1.oauth.user_connection_service.stamp_last_synced_at"),
        patch("app.integrations.celery.tasks.sync_vendor_data") as task,
    ):
        response = client.get(
            "/api/v1/oauth/polar/callback", params={"code": "c", "state": "s"}, follow_redirects=False
        )
        assert response.status_code == 303

    task.delay.assert_called_once()
    assert task.delay.call_args.kwargs["is_historical"] is True
    assert task.delay.call_args.kwargs["ignore_pull_interval"] is True
