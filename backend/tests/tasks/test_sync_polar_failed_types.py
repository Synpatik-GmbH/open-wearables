"""2.96.6.1 (spec cause 3, AC4, AC5): a Polar pull with a failed data type, through the task.

The failed type is recorded FAILED with a code and no text, and the connection's cursor does
not move past the pull. Polar is faked at ``_make_api_request``; storage is real.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.sync_vendor_data_task import sync_vendor_data
from app.models import SyncRun, SyncRunDataType, UserConnection
from app.schemas.auth import ConnectionStatus
from app.schemas.sync_status import SyncStatus
from app.services.providers.polar.data_247 import Polar247Data
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.polar.test_polar_no_lost_nights import FakePolar


@pytest.fixture
def polar() -> FakePolar:
    return FakePolar()


@patch("app.integrations.celery.tasks.sync_vendor_data_task.SessionLocal")
class TestTheSyncRun:
    """The run record and the cursor, for a real Polar pull through the task."""

    def _pull(self, db: Session, session_local: MagicMock, polar: FakePolar) -> UUID:
        user = UserFactory()
        connection = UserConnectionFactory(user=user, provider="polar", status=ConnectionStatus.ACTIVE)
        connection.last_synced_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        session_local.return_value.__enter__.return_value = db
        session_local.return_value.__exit__.return_value = None
        with (
            # The run and its data types are written through their own session. Live runs are
            # stored only with persist_live_sync_runs (off by default and in both environments).
            patch("app.services.sync_status_service.SessionLocal", session_local),
            patch("app.services.sync_status_service.settings.persist_live_sync_runs", True),
            patch.object(Polar247Data, "_make_api_request", side_effect=polar),
            patch("app.services.providers.templates.base_workouts.make_authenticated_request", return_value=[]),
        ):
            sync_vendor_data(str(user.id), providers=["polar"])
        return connection.id

    def _connection_cursor(self, db: Session, connection_id: UUID) -> datetime | None:
        db.expire_all()
        return db.get(UserConnection, connection_id).last_synced_at

    def test_a_failed_type_is_recorded_failed_with_a_code_and_no_text(
        self, session_local: MagicMock, db: Session, polar: FakePolar, mock_celery_app: MagicMock
    ) -> None:
        # AC5: the failure record holds a code and no free-text message.
        polar.failing_endpoints = {"/v3/users/nightly-recharge"}

        self._pull(db, session_local, polar)

        row = (
            db.query(SyncRunDataType)
            .join(SyncRun, SyncRunDataType.run_id == SyncRun.id)
            .filter(SyncRun.provider == "polar", SyncRunDataType.data_type == "nightly_recharge")
            .one()
        )
        assert row.status == SyncStatus.FAILED
        assert row.error_code == "HTTPException_503"
        assert row.error is None

    def test_the_cursor_does_not_move_past_a_pull_with_a_failed_type(
        self, session_local: MagicMock, db: Session, polar: FakePolar, mock_celery_app: MagicMock
    ) -> None:
        polar.failing_endpoints = {"/v3/users/nightly-recharge"}
        started = datetime.now(timezone.utc)

        connection_id = self._pull(db, session_local, polar)

        assert self._connection_cursor(db, connection_id) < started

    def test_the_cursor_moves_after_a_pull_in_which_nothing_failed(
        self, session_local: MagicMock, db: Session, polar: FakePolar, mock_celery_app: MagicMock
    ) -> None:
        started = datetime.now(timezone.utc)

        connection_id = self._pull(db, session_local, polar)

        assert self._connection_cursor(db, connection_id) >= started

    def test_a_failure_is_logged_as_a_code_without_the_exception_text(
        self,
        session_local: MagicMock,
        db: Session,
        polar: FakePolar,
        mock_celery_app: MagicMock,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Spec cause 3 (R-2026-10-06-b): logged as a code, not free text.
        polar.failing_endpoints = {"/v3/users/nightly-recharge"}
        polar.failing_nights = {datetime.now(timezone.utc).date()}
        polar.nights = [datetime.now(timezone.utc).date()]

        self._pull(db, session_local, polar)

        logged = caplog.text + capsys.readouterr().out
        assert "HTTPException_503" in logged
        assert "upstream said something" not in logged
        assert "night failed" not in logged
