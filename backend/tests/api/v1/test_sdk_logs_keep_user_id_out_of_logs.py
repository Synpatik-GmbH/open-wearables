"""SDK log ingestion writes no user id into the logs (fork data-protection control).

The log window has no erasure path: a deletion request reaches the database, not the
log storage. So nothing this endpoint logs may carry the user id, on any path it can
take. The request line (``http_request``, which carries the URL path) is the one
accepted copy, disclosed in FORK-DELTA.md, and is set aside here by name.
"""

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.schemas.sync_status import SyncRunWrite
from tests.factories import ApiKeyFactory, UserFactory

ENDPOINT = "/api/v1/sdk/users/{user_id}/logs"

SYNC_START_EVENT = {
    "eventType": "historical_data_sync_start",
    "timestamp": "2026-04-09T10:00:00Z",
    "dataTypeCounts": [{"type": "HKQuantityTypeIdentifierStepCount", "count": 200}],
    "timeRange": {"startDate": "2026-01-09T00:00:00Z", "endDate": "2026-04-09T10:00:00Z"},
}


def _post_sync_start(client: TestClient) -> str:
    user = UserFactory()
    api_key = ApiKeyFactory()
    response = client.post(
        ENDPOINT.format(user_id=user.id),
        headers={"X-Open-Wearables-API-Key": api_key.plain_key},
        json={
            "sdkVersion": "1.2.0",
            "provider": "apple",
            "syncSessionId": "session-1",
            "events": [SYNC_START_EVENT],
        },
    )
    assert response.status_code == 202
    return str(user.id)


def _logged(capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> str:
    out, err = capfd.readouterr()
    lines = (out + err + caplog.text).splitlines()
    return "\n".join(line for line in lines if '"message": "http_request"' not in line)


class TestSdkLogsKeepUserIdOutOfLogs:
    @patch("app.api.routes.v1.sdk_logs.store_raw_payload")
    def test_sync_start_logs_the_run_but_not_the_user(
        self,
        mock_store: MagicMock,
        client: TestClient,
        db: Session,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level("DEBUG")
        user_id = _post_sync_start(client)

        logged = _logged(capfd, caplog)
        # The line is still written, so an empty capture cannot pass for a clean one.
        assert '"action": "sync_status"' in logged
        assert "sdk_session-1" in logged
        assert user_id not in logged

    @patch("app.api.routes.v1.sdk_logs.store_raw_payload")
    def test_a_failed_run_write_does_not_log_the_user(
        self,
        mock_store: MagicMock,
        client: TestClient,
        db: Session,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level("DEBUG")

        # A database error names the statement's parameters, as this one does.
        def fail(_db: object, run: SyncRunWrite) -> None:
            raise RuntimeError(f"insert failed [parameters: {{'user_id': '{run.user_id}'}}]")

        with patch("app.services.sync_status_service.sync_run_repository.upsert_run", side_effect=fail):
            user_id = _post_sync_start(client)

        logged = _logged(capfd, caplog)
        assert '"action": "sync_run_persist_failed"' in logged
        assert user_id not in logged

    @patch("app.api.routes.v1.sdk_logs.store_raw_payload")
    def test_a_failed_redis_write_does_not_log_the_user(
        self,
        mock_store: MagicMock,
        client: TestClient,
        db: Session,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level("DEBUG")

        # A pipeline error quotes the failed command, key and payload included.
        class FailingPipeline:
            def __init__(self) -> None:
                self.first_key = ""

            def __getattr__(self, _name: str) -> Callable[..., None]:
                def record(key: str = "", *_args: object, **_kwargs: object) -> None:
                    self.first_key = self.first_key or key

                return record

            def execute(self) -> None:
                raise RuntimeError(f"Command # 1 (LPUSH {self.first_key}) of pipeline caused error")

        failing = MagicMock()
        failing.pipeline.return_value = FailingPipeline()
        with patch("app.services.sync_status_service.get_redis_client", return_value=failing):
            user_id = _post_sync_start(client)

        logged = _logged(capfd, caplog)
        assert "Failed to emit sync status event" in logged
        assert user_id not in logged
