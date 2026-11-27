"""
Tests for finalize_stale_sleeps Celery task.

Tests the background task that finalizes sleep sessions that have been
inactive in Redis for longer than the configured threshold.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.finalize_stale_sleep_task import finalize_stale_sleeps
from app.schemas.providers.mobile_sdk import SleepState
from app.services.sdk.sleep_service import active_users_key


class TestFinalizeStaleSleepsTask:
    """Test suite for finalize_stale_sleeps task."""

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_no_active_users(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task completes successfully when no active sleep sessions exist."""
        # Arrange
        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = []  # No active users
        mock_redis_client_func.return_value = mock_redis

        # Act
        result = finalize_stale_sleeps()

        # Assert
        assert result is None  # Task should complete without errors
        mock_redis.smembers.assert_called_once()

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_with_stale_session(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task finalizes sleep sessions older than threshold."""
        # Arrange
        user_id = str(uuid4())
        now = datetime.now(timezone.utc)
        stale_timestamp = now - timedelta(hours=2)  # Older than 1 hour threshold

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        # Mock sleep state with old timestamp
        mock_sleep_state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=900,
            awake_seconds=300,
            light_seconds=3600,
            deep_seconds=1800,
            rem_seconds=1200,
            stages=[],
        )
        mock_load_state.return_value = mock_sleep_state

        # Act
        finalize_stale_sleeps()

        # Assert
        mock_load_state.assert_called_once_with(user_id)
        mock_finish_sleep.assert_called_once_with(db, user_id, mock_sleep_state)

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_skips_recent_session(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task does not finalize recent sleep sessions."""
        # Arrange
        user_id = str(uuid4())
        now = datetime.now(timezone.utc)
        recent_timestamp = now - timedelta(minutes=30)  # Less than 1 hour threshold

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        # Mock recent sleep state
        mock_sleep_state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider=None,
            start_time=recent_timestamp,
            end_time=recent_timestamp,
            last_start_timestamp=recent_timestamp,
            last_end_timestamp=recent_timestamp,
            in_bed_seconds=900,
            awake_seconds=0,
            light_seconds=900,
            deep_seconds=0,
            rem_seconds=0,
            stages=[],
        )
        mock_load_state.return_value = mock_sleep_state

        # Act
        finalize_stale_sleeps()

        # Assert
        mock_load_state.assert_called_once_with(user_id)
        mock_finish_sleep.assert_not_called()  # Should not finalize recent sessions

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_handles_missing_state(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task gracefully handles users with no sleep state."""
        # Arrange
        user_id = str(uuid4())

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        mock_load_state.return_value = None  # State not found

        # Act
        result = finalize_stale_sleeps()

        # Assert
        assert result is None
        mock_load_state.assert_called_once_with(user_id)

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_handles_multiple_users(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task processes multiple users with stale sessions."""
        # Arrange
        user_id_1 = str(uuid4())
        user_id_2 = str(uuid4())
        user_id_3 = str(uuid4())
        now = datetime.now(timezone.utc)
        stale_timestamp = now - timedelta(hours=2)

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id_1, user_id_2, user_id_3]
        mock_redis_client_func.return_value = mock_redis

        # User 1: Stale session (should finalize)
        state_1 = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=900,
            awake_seconds=0,
            light_seconds=3600,
            deep_seconds=1800,
            rem_seconds=1200,
            stages=[],
        )

        # User 2: No state (should skip)
        state_2 = None

        # User 3: Stale session (should finalize)
        state_3 = SleepState(
            uuid=str(uuid4()),
            source_name="iPhone",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=600,
            awake_seconds=600,
            light_seconds=0,
            deep_seconds=0,
            rem_seconds=0,
            stages=[],
        )

        mock_load_state.side_effect = [state_1, state_2, state_3]

        # Act
        finalize_stale_sleeps()

        # Assert
        assert mock_load_state.call_count == 3
        assert mock_finish_sleep.call_count == 2  # Only user 1 and 3

        mock_finish_sleep.assert_any_call(db, user_id_1, state_1)
        mock_finish_sleep.assert_any_call(db, user_id_3, state_3)

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.log_and_capture_error")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_handles_finish_error(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        mock_log_error: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task continues processing other users if one finalization fails."""
        # Arrange
        user_id_1 = str(uuid4())
        user_id_2 = str(uuid4())
        now = datetime.now(timezone.utc)
        stale_timestamp = now - timedelta(hours=2)

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id_1, user_id_2]
        mock_redis_client_func.return_value = mock_redis

        state_1 = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=900,
            awake_seconds=0,
            light_seconds=3600,
            deep_seconds=0,
            rem_seconds=0,
            stages=[],
        )

        state_2 = SleepState(
            uuid=str(uuid4()),
            source_name="iPhone",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=0,
            awake_seconds=0,
            light_seconds=0,
            deep_seconds=0,
            rem_seconds=1800,
            stages=[],
        )

        mock_load_state.side_effect = [state_1, state_2]

        # User 1 finalization fails
        mock_finish_sleep.side_effect = [Exception("Database error"), None]

        # Act
        finalize_stale_sleeps()

        # Assert
        assert mock_finish_sleep.call_count == 2
        mock_log_error.assert_called_once()  # Error should be logged for user 1

        # Verify user 2 was still processed despite user 1 error
        mock_finish_sleep.assert_any_call(db, user_id_2, state_2)

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_handles_malformed_state(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task skips users whose sleep state cannot be parsed from Redis."""
        # Arrange
        user_id = str(uuid4())

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        # Simulate load_sleep_state returning None (as it does when state is malformed/unparseable)
        mock_load_state.return_value = None

        # Act - should not crash
        result = finalize_stale_sleeps()

        # Assert
        assert result is None  # Task completes and skips the malformed user
        mock_load_state.assert_called_once_with(user_id)


class TestFinalizeQuietAppleWatchSleep:
    """FORK (port of upstream #1892): the periodic task applies the same rule as the upload path."""

    def _run(self, db: Session, state: SleepState | None) -> tuple[MagicMock, MagicMock, str]:
        user_id = str(uuid4())
        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        with (
            patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal") as mock_session_local,
            patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client", return_value=mock_redis),
            patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state", return_value=state),
            patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep") as mock_finish_sleep,
        ):
            mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
            mock_session_local.return_value.__exit__ = MagicMock(return_value=None)
            finalize_stale_sleeps()
        return mock_redis, mock_finish_sleep, user_id

    def _state(self, received_ago: int, watch_sleep: bool) -> SleepState:
        now = datetime.now(timezone.utc)
        woke_up = now - timedelta(minutes=40)
        return SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model="Watch7,1",
            provider="apple",
            start_time=woke_up - timedelta(hours=7),
            end_time=woke_up,
            last_start_timestamp=woke_up - timedelta(minutes=10),
            last_end_timestamp=woke_up,
            last_received_at=now - timedelta(minutes=received_ago),
            has_apple_watch_sleep=watch_sleep,
        )

    def test_closes_a_quiet_apple_watch_session(self, db: Session, mock_celery_app: MagicMock) -> None:
        state = self._state(received_ago=20, watch_sleep=True)

        _, mock_finish_sleep, user_id = self._run(db, state)

        mock_finish_sleep.assert_called_once_with(db, user_id, state)

    @pytest.mark.parametrize(
        ("received_ago", "watch_sleep"),
        [(5, True), (20, False)],
        ids=["watch-still-uploading", "not-apple-watch"],
    )
    def test_leaves_a_session_open_until_its_rule_is_met(
        self, received_ago: int, watch_sleep: bool, db: Session, mock_celery_app: MagicMock
    ) -> None:
        _, mock_finish_sleep, _ = self._run(db, self._state(received_ago=received_ago, watch_sleep=watch_sleep))

        mock_finish_sleep.assert_not_called()

    def test_a_user_whose_session_expired_leaves_the_active_set(self, db: Session, mock_celery_app: MagicMock) -> None:
        """Upstream #1892: an expired Redis state left the user in sleep:active_users forever, and at a
        5-minute interval every one of them is re-read 12 times as often."""
        mock_redis, mock_finish_sleep, user_id = self._run(db, None)

        mock_redis.srem.assert_called_once_with(active_users_key(), user_id)
        mock_finish_sleep.assert_not_called()

    def test_a_live_session_stays_in_the_active_set(self, db: Session, mock_celery_app: MagicMock) -> None:
        mock_redis, _, _ = self._run(db, self._state(received_ago=5, watch_sleep=True))

        mock_redis.srem.assert_not_called()
