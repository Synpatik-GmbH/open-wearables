"""2.96.6.1 (spec R1, AC1): the pull queued at connect is not held back by the pull interval."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient


def test_the_pull_queued_at_connect_ignores_the_pull_interval(client: TestClient) -> None:
    strategy = MagicMock()
    strategy.capabilities.webhook_callback = False
    strategy.capabilities.rest_pull = True
    strategy.oauth.handle_callback.return_value = MagicMock(user_id=uuid4(), redirect_uri=None)

    with (
        patch("app.api.routes.v1.oauth.get_oauth_strategy", return_value=strategy),
        patch("app.api.routes.v1.oauth.user_connection_service.stamp_last_synced_at"),
        patch("app.integrations.celery.tasks.sync_vendor_data") as task,
    ):
        client.get("/api/v1/oauth/polar/callback", params={"code": "c", "state": "s"}, follow_redirects=False)

    task.delay.assert_called_once()
    assert task.delay.call_args.kwargs["is_historical"] is True
    assert task.delay.call_args.kwargs["ignore_pull_interval"] is True
