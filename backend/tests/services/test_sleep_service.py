"""
Tests for Apple HealthKit sleep service processing.

Tests the sleep pipeline (handle_sleep_data, _apply_transition, _calculate_final_metrics,
finish_sleep) using synthetic payloads modeled after real Apple HealthKit SDK data.

Apple Watch sleep data patterns:
- Older Apple Watch (pre-watchOS 9): only "in_bed" and "sleeping" stages
- Newer Apple Watch (watchOS 9+): "in_bed", "awake", "light", "deep", "rem" stages
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.constants.sleep import SleepStageType
from app.models import DataSource, EventRecord, SleepDetails
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
)
from app.schemas.providers.mobile_sdk import (
    SleepState,
    SleepStateStage,
    SyncRequest,
)
from app.services.sdk.sleep_service import (
    _calculate_final_metrics,
    finish_sleep,
    handle_sleep_data,
    is_ready_to_finish,
)
from app.services.sdk_token_service import create_sdk_user_token
from tests.factories import UserFactory


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Synthetic payload: older Apple Watch (pre-watchOS 9) with sleeping + in_bed
# Mimics pattern: Watch sends "sleeping" segments, iPhone sends "in_bed"
# ---------------------------------------------------------------------------
OLD_WATCH_PAYLOAD = {
    "provider": "apple",
    "sdkVersion": "0.5.0",
    "syncTimestamp": "2026-03-11T13:28:04Z",
    "data": {
        "records": [],
        "workouts": [],
        "sleep": [
            {
                "id": "aaaa1111-0000-0000-0000-000000000001",
                "parentId": None,
                "stage": "sleeping",
                "startDate": "2026-03-10T23:00:00Z",
                "endDate": "2026-03-10T23:50:00Z",
                "source": {
                    "device_type": "watch",
                    "device_model": "Watch3,3",
                },
            },
            {
                "id": "aaaa1111-0000-0000-0000-000000000002",
                "parentId": None,
                "stage": "sleeping",
                "startDate": "2026-03-10T23:52:00Z",
                "endDate": "2026-03-11T00:45:00Z",
                "source": {
                    "device_type": "watch",
                    "device_model": "Watch3,3",
                },
            },
            {
                "id": "aaaa1111-0000-0000-0000-000000000003",
                "parentId": None,
                "stage": "sleeping",
                "startDate": "2026-03-11T00:47:00Z",
                "endDate": "2026-03-11T01:30:00Z",
                "source": {
                    "device_type": "watch",
                    "device_model": "Watch3,3",
                },
            },
            {
                "id": "aaaa1111-0000-0000-0000-000000000004",
                "parentId": None,
                "stage": "in_bed",
                "startDate": "2026-03-10T22:55:00Z",
                "endDate": "2026-03-11T01:35:00Z",
                "source": {
                    "device_type": "phone",
                    "device_model": "iPhone15,2",
                },
            },
        ],
    },
}

# ---------------------------------------------------------------------------
# Synthetic payload: detailed stages (watchOS 9+ style)
# ---------------------------------------------------------------------------
DETAILED_STAGES_PAYLOAD = {
    "provider": "apple",
    "sdkVersion": "1.0.0",
    "syncTimestamp": "2026-03-11T13:28:04Z",
    "data": {
        "records": [],
        "workouts": [],
        "sleep": [
            {
                "id": "A001",
                "stage": "in_bed",
                "startDate": "2026-03-10T22:00:00Z",
                "endDate": "2026-03-11T06:00:00Z",
                "source": {"device_type": "phone", "device_model": "iPhone15,2"},
            },
            {
                "id": "A002",
                "stage": "light",
                "startDate": "2026-03-10T22:15:00Z",
                "endDate": "2026-03-10T23:00:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A003",
                "stage": "deep",
                "startDate": "2026-03-10T23:00:00Z",
                "endDate": "2026-03-11T00:30:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A004",
                "stage": "rem",
                "startDate": "2026-03-11T00:30:00Z",
                "endDate": "2026-03-11T01:15:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A005",
                "stage": "awake",
                "startDate": "2026-03-11T01:15:00Z",
                "endDate": "2026-03-11T01:25:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A006",
                "stage": "deep",
                "startDate": "2026-03-11T01:25:00Z",
                "endDate": "2026-03-11T02:30:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A007",
                "stage": "light",
                "startDate": "2026-03-11T02:30:00Z",
                "endDate": "2026-03-11T04:00:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A008",
                "stage": "rem",
                "startDate": "2026-03-11T04:00:00Z",
                "endDate": "2026-03-11T05:00:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A009",
                "stage": "light",
                "startDate": "2026-03-11T05:00:00Z",
                "endDate": "2026-03-11T05:45:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
        ],
    },
}


class TestCalculateFinalMetrics:
    """Tests for _calculate_final_metrics with different stage combinations."""

    def test_sleeping_stages_only(self) -> None:
        """Older Apple Watch data: only 'sleeping' stages should NOT map to deep."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-10T23:00:00Z"),
                end_time=_dt("2026-03-10T23:50:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-10T23:52:00Z"),
                end_time=_dt("2026-03-11T00:45:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-11T00:47:00Z"),
                end_time=_dt("2026-03-11T01:30:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # "sleeping" should go to sleeping_seconds, NOT deep_seconds
        assert metrics["deep_seconds"] == 0
        assert metrics["light_seconds"] == 0
        assert metrics["rem_seconds"] == 0
        assert metrics["sleeping_seconds"] > 0

        # All cleaned stages should be SLEEPING type
        for s in cleaned:
            assert s.stage == SleepStageType.SLEEPING

        # Total sleeping time: 50min + 53min + 43min = 146min = 8760s
        total_sleeping = metrics["sleeping_seconds"]
        assert total_sleeping == pytest.approx(8760, abs=60)

    def test_sleeping_plus_in_bed(self) -> None:
        """Mixed old-style data: sleeping (watch) + in_bed (phone)."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-10T23:00:00Z"),
                end_time=_dt("2026-03-11T01:30:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.IN_BED,
                start_time=_dt("2026-03-10T22:55:00Z"),
                end_time=_dt("2026-03-11T01:35:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # deep should be 0 — sleeping is not deep
        assert metrics["deep_seconds"] == 0
        assert metrics["sleeping_seconds"] > 0
        # in_bed calculated from in_bed intervals
        assert metrics["in_bed_seconds"] > 0
        # Cleaned stages should only include sleeping (not in_bed)
        assert all(s.stage == SleepStageType.SLEEPING for s in cleaned)

    def test_detailed_stages(self) -> None:
        """Modern Apple Watch data with deep/light/rem/awake breakdown."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.LIGHT, start_time=_dt("2026-03-10T22:15:00Z"), end_time=_dt("2026-03-10T23:00:00Z")
            ),
            SleepStateStage(
                stage=SleepStageType.DEEP, start_time=_dt("2026-03-10T23:00:00Z"), end_time=_dt("2026-03-11T00:30:00Z")
            ),
            SleepStateStage(
                stage=SleepStageType.REM, start_time=_dt("2026-03-11T00:30:00Z"), end_time=_dt("2026-03-11T01:15:00Z")
            ),
            SleepStateStage(
                stage=SleepStageType.AWAKE, start_time=_dt("2026-03-11T01:15:00Z"), end_time=_dt("2026-03-11T01:25:00Z")
            ),
            SleepStateStage(
                stage=SleepStageType.DEEP, start_time=_dt("2026-03-11T01:25:00Z"), end_time=_dt("2026-03-11T02:30:00Z")
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        assert metrics["sleeping_seconds"] == 0
        assert metrics["light_seconds"] == 45 * 60  # 45 min
        assert metrics["deep_seconds"] == (90 + 65) * 60  # 155 min
        assert metrics["rem_seconds"] == 45 * 60  # 45 min
        assert metrics["awake_seconds"] == 10 * 60  # 10 min

    def test_in_bed_fallback_includes_sleeping(self) -> None:
        """When no in_bed stages exist, in_bed_seconds should sum all sleep types."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-11T00:00:00Z"),
                end_time=_dt("2026-03-11T06:00:00Z"),
            ),
        ]

        metrics, _ = _calculate_final_metrics(stages)

        # No in_bed stages → fallback includes sleeping_seconds
        assert metrics["in_bed_seconds"] == metrics["sleeping_seconds"] + metrics["awake_seconds"]

    def test_empty_stages(self) -> None:
        """Empty stages list should return zero metrics."""
        metrics, cleaned = _calculate_final_metrics([])

        assert metrics["sleeping_seconds"] == 0
        assert metrics["deep_seconds"] == 0
        assert metrics["in_bed_seconds"] == 0
        assert cleaned == []

    def test_only_in_bed_treated_as_sleeping(self) -> None:
        """When only in_bed stages exist (no sleep phases), treat in_bed as sleeping."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.IN_BED,
                start_time=_dt("2026-04-10T22:30:00Z"),
                end_time=_dt("2026-04-11T06:00:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # in_bed should be converted to sleeping
        assert metrics["sleeping_seconds"] == 7.5 * 3600
        assert metrics["deep_seconds"] == 0
        assert metrics["light_seconds"] == 0
        assert metrics["rem_seconds"] == 0
        # in_bed_seconds still calculated from original in_bed intervals
        assert metrics["in_bed_seconds"] == 7.5 * 3600
        # Hypnogram should show sleeping, not in_bed
        assert len(cleaned) == 1
        assert cleaned[0].stage == SleepStageType.SLEEPING

    def test_detailed_plus_sleeping_wrapper_excludes_sleeping(self) -> None:
        """When detailed phases + sleeping wrapper coexist, sleeping is dropped."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-04-10T22:00:00Z"),
                end_time=_dt("2026-04-11T06:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.LIGHT,
                start_time=_dt("2026-04-10T22:10:00Z"),
                end_time=_dt("2026-04-10T23:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.DEEP,
                start_time=_dt("2026-04-10T23:00:00Z"),
                end_time=_dt("2026-04-11T01:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.REM,
                start_time=_dt("2026-04-11T01:00:00Z"),
                end_time=_dt("2026-04-11T02:00:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # sleeping wrapper must NOT be counted
        assert metrics["sleeping_seconds"] == 0
        assert metrics["light_seconds"] == 50 * 60
        assert metrics["deep_seconds"] == 2 * 3600
        assert metrics["rem_seconds"] == 1 * 3600
        # Hypnogram should not contain sleeping
        stage_types = {s.stage for s in cleaned}
        assert SleepStageType.SLEEPING not in stage_types
        assert SleepStageType.IN_BED not in stage_types

    def test_detailed_plus_sleeping_plus_in_bed(self) -> None:
        """Full modern scenario: in_bed + sleeping wrapper + detailed phases."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.IN_BED,
                start_time=_dt("2026-04-10T22:00:00Z"),
                end_time=_dt("2026-04-11T06:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-04-10T22:00:00Z"),
                end_time=_dt("2026-04-11T06:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.DEEP,
                start_time=_dt("2026-04-10T22:30:00Z"),
                end_time=_dt("2026-04-11T00:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.LIGHT,
                start_time=_dt("2026-04-11T00:00:00Z"),
                end_time=_dt("2026-04-11T02:00:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # Only detailed phases should be counted
        assert metrics["sleeping_seconds"] == 0
        assert metrics["deep_seconds"] == 1.5 * 3600
        assert metrics["light_seconds"] == 2 * 3600
        # in_bed still calculated from original intervals
        assert metrics["in_bed_seconds"] == 8 * 3600
        # Hypnogram: only deep + light
        stage_types = {s.stage for s in cleaned}
        assert stage_types == {SleepStageType.DEEP, SleepStageType.LIGHT}


class TestFinishSleep:
    """Tests for finish_sleep with different stage compositions."""

    @patch("app.services.sdk.sleep_service.event_record_service")
    @patch("app.services.sdk.sleep_service.delete_sleep_state")
    def test_finish_sleep_with_sleeping_stages(
        self,
        mock_delete_state: MagicMock,
        mock_event_service: MagicMock,
        db: Session,
    ) -> None:
        """Finish sleep with old-style 'sleeping' data should set correct totals."""
        user_id = str(uuid4())
        mock_record = MagicMock()
        mock_record.id = uuid4()
        mock_event_service.create.return_value = mock_record
        mock_event_service.find_adjacent_sleep_record.return_value = None

        state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model="Watch3,3",
            provider="apple",
            start_time=_dt("2026-03-15T23:00:00Z"),
            end_time=_dt("2026-03-16T01:30:00Z"),
            last_start_timestamp=_dt("2026-03-16T00:47:00Z"),
            last_end_timestamp=_dt("2026-03-16T01:30:00Z"),
            sleeping_seconds=8760.0,
            stages=[
                SleepStateStage(
                    stage=SleepStageType.SLEEPING,
                    start_time=_dt("2026-03-15T23:00:00Z"),
                    end_time=_dt("2026-03-15T23:50:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.SLEEPING,
                    start_time=_dt("2026-03-15T23:52:00Z"),
                    end_time=_dt("2026-03-16T00:45:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.SLEEPING,
                    start_time=_dt("2026-03-16T00:47:00Z"),
                    end_time=_dt("2026-03-16T01:30:00Z"),
                ),
            ],
        )

        finish_sleep(db, user_id, state)

        # Verify create was called
        mock_event_service.create.assert_called_once()
        mock_event_service.create_detail.assert_called_once()

        # Check the detail payload
        detail_call = mock_event_service.create_detail.call_args
        detail = detail_call[0][1]  # second positional arg

        # Total duration should include sleeping time
        assert detail.sleep_total_duration_minutes > 0
        # Deep/rem/light should all be 0 (no breakdown available)
        assert detail.sleep_deep_minutes == 0
        assert detail.sleep_rem_minutes == 0
        assert detail.sleep_light_minutes == 0
        # Stages should be present
        assert detail.sleep_stages is not None
        assert len(detail.sleep_stages) == 3
        assert all(s.stage == SleepStageType.SLEEPING for s in detail.sleep_stages)

    @patch("app.services.sdk.sleep_service.event_record_service")
    @patch("app.services.sdk.sleep_service.delete_sleep_state")
    def test_finish_sleep_with_detailed_stages(
        self,
        mock_delete_state: MagicMock,
        mock_event_service: MagicMock,
        db: Session,
    ) -> None:
        """Finish sleep with detailed stages should set deep/rem/light correctly."""
        user_id = str(uuid4())
        mock_record = MagicMock()
        mock_record.id = uuid4()
        mock_event_service.create.return_value = mock_record
        mock_event_service.find_adjacent_sleep_record.return_value = None

        state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model="Watch7,1",
            provider="apple",
            start_time=_dt("2026-03-10T22:15:00Z"),
            end_time=_dt("2026-03-11T02:30:00Z"),
            last_start_timestamp=_dt("2026-03-11T01:25:00Z"),
            last_end_timestamp=_dt("2026-03-11T02:30:00Z"),
            light_seconds=2700.0,  # 45 min
            deep_seconds=9300.0,  # 155 min
            rem_seconds=2700.0,  # 45 min
            awake_seconds=600.0,  # 10 min
            stages=[
                SleepStateStage(
                    stage=SleepStageType.LIGHT,
                    start_time=_dt("2026-03-10T22:15:00Z"),
                    end_time=_dt("2026-03-10T23:00:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.DEEP,
                    start_time=_dt("2026-03-10T23:00:00Z"),
                    end_time=_dt("2026-03-11T00:30:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.REM,
                    start_time=_dt("2026-03-11T00:30:00Z"),
                    end_time=_dt("2026-03-11T01:15:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.AWAKE,
                    start_time=_dt("2026-03-11T01:15:00Z"),
                    end_time=_dt("2026-03-11T01:25:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.DEEP,
                    start_time=_dt("2026-03-11T01:25:00Z"),
                    end_time=_dt("2026-03-11T02:30:00Z"),
                ),
            ],
        )

        finish_sleep(db, user_id, state)

        detail = mock_event_service.create_detail.call_args[0][1]

        assert detail.sleep_deep_minutes == 155
        assert detail.sleep_light_minutes == 45
        assert detail.sleep_rem_minutes == 45
        assert detail.sleep_awake_minutes == 10
        assert detail.sleep_total_duration_minutes == 245  # light+deep+rem (no sleeping)


class TestHandleSleepDataIntegration:
    """Integration tests for handle_sleep_data with real payload structures."""

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.sdk.sleep_service.event_record_service")
    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_handle_real_payload_sleeping_stages(
        self,
        mock_redis_func: MagicMock,
        mock_event_service: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """Process a synthetic payload with in_bed + sleeping stages.

        Modeled after older Apple Watch pattern:
        - 3 sleeping segments (Watch3,3)
        - 1 in_bed segment (iPhone15,2)
        All within gap threshold → single session.
        """
        user_id = str(uuid4())

        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # No existing state
        mock_redis_func.return_value = mock_redis

        mock_record = MagicMock()
        mock_record.id = uuid4()
        mock_event_service.create.return_value = mock_record
        mock_event_service.find_adjacent_sleep_record.return_value = None

        request = SyncRequest.model_validate(OLD_WATCH_PAYLOAD)

        handle_sleep_data(db, request, user_id)

        # Sleep state should be saved to Redis (session not yet finalized —
        # that happens via finalize_stale_sleeps.delay())
        assert mock_redis.set.called

        # The finalize task should be dispatched
        mock_finalize.delay.assert_called_once()

        # Verify saved state: grab the last set() call's value
        last_set_call = mock_redis.set.call_args_list[-1]
        state_json = last_set_call[0][1]  # second positional arg
        state = SleepState.model_validate_json(state_json)

        # sleeping_seconds should be populated, NOT deep_seconds
        assert state.sleeping_seconds > 0
        assert state.deep_seconds == 0
        assert state.light_seconds == 0
        assert state.rem_seconds == 0

        # All entries are within the gap threshold so they merge into one session.
        sleeping_stages = [s for s in state.stages if s.stage == SleepStageType.SLEEPING]
        in_bed_stages = [s for s in state.stages if s.stage == SleepStageType.IN_BED]
        assert len(sleeping_stages) >= 1
        assert len(in_bed_stages) == 1

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.sdk.sleep_service.event_record_service")
    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_handle_detailed_stages_payload(
        self,
        mock_redis_func: MagicMock,
        mock_event_service: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """Process a modern payload with detailed sleep stages."""
        user_id = str(uuid4())

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis_func.return_value = mock_redis

        mock_record = MagicMock()
        mock_record.id = uuid4()
        mock_event_service.create.return_value = mock_record
        mock_event_service.find_adjacent_sleep_record.return_value = None

        request = SyncRequest.model_validate(DETAILED_STAGES_PAYLOAD)

        handle_sleep_data(db, request, user_id)

        # Verify saved state
        last_set_call = mock_redis.set.call_args_list[-1]
        state = SleepState.model_validate_json(last_set_call[0][1])

        assert state.sleeping_seconds == 0
        assert state.deep_seconds > 0
        assert state.light_seconds > 0
        assert state.rem_seconds > 0
        assert state.awake_seconds > 0

        # Verify stage types
        stage_types = {s.stage for s in state.stages}
        assert SleepStageType.DEEP in stage_types
        assert SleepStageType.LIGHT in stage_types
        assert SleepStageType.REM in stage_types
        assert SleepStageType.AWAKE in stage_types


class TestSDKSyncEndpointSleep:
    """Test the /sdk/users/{user_id}/sync endpoint with sleep payloads."""

    def test_sync_endpoint_accepts_sleeping_stage(
        self,
        client: MagicMock,
        db: Session,
    ) -> None:
        """Endpoint should validate payload with 'sleeping' stage (older Apple Watch)."""
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        token = create_sdk_user_token("test_app", user_id)

        with patch("app.api.routes.v1.sdk_sync.process_sdk_upload") as mock_task:
            mock_task.delay.return_value = None

            response = client.post(
                "/api/v1/sdk/users/" + user_id + "/sync/",
                headers={"Authorization": f"Bearer {token}"},
                json=OLD_WATCH_PAYLOAD,
            )

        assert response.status_code == 202
        data = response.json()
        assert data["status_code"] == 202
        mock_task.delay.assert_called_once()

    def test_sync_endpoint_accepts_detailed_stages(
        self,
        client: MagicMock,
        db: Session,
    ) -> None:
        """Endpoint should validate payload with detailed sleep stages."""
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        token = create_sdk_user_token("test_app", user_id)

        with patch("app.api.routes.v1.sdk_sync.process_sdk_upload") as mock_task:
            mock_task.delay.return_value = None

            response = client.post(
                "/api/v1/sdk/users/" + user_id + "/sync/",
                headers={"Authorization": f"Bearer {token}"},
                json=DETAILED_STAGES_PAYLOAD,
            )

        assert response.status_code == 202
        data = response.json()
        assert data["status_code"] == 202


class TestNoIntermediateRedisSaves:
    """Regression test: Redis state must only be saved once per batch, not per stage.

    Previously, save_sleep_state was called inside the per-stage loop, exposing
    partially-accumulated intermediate states to the concurrent finalize_stale_sleeps
    task.  That task could read a partial state, decide it was stale, and finalize it
    — producing a duplicate (subset) sleep record.  Moving the save outside the loop
    prevents this race condition.
    """

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.sdk.sleep_service.event_record_service")
    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_redis_set_called_once_per_batch(
        self,
        mock_redis_func: MagicMock,
        mock_event_service: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """Redis .set() should be called exactly once after processing all stages."""
        user_id = str(uuid4())

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis_func.return_value = mock_redis

        mock_record = MagicMock()
        mock_record.id = uuid4()
        mock_event_service.create.return_value = mock_record
        mock_event_service.find_adjacent_sleep_record.return_value = None

        request = SyncRequest.model_validate(DETAILED_STAGES_PAYLOAD)

        handle_sleep_data(db, request, user_id)

        # Count how many times set() was called (each call = one Redis state save).
        # With the fix, this should be exactly 1 — after the loop finishes.
        # Before the fix, it was called once per stage (9 times for this payload).
        set_calls = mock_redis.set.call_args_list
        assert len(set_calls) == 1, (
            f"Expected exactly 1 Redis save per batch, got {len(set_calls)}. "
            "Intermediate saves expose partial state to finalize_stale_sleeps."
        )

        # Verify the single saved state contains ALL stages from the payload
        state = SleepState.model_validate_json(set_calls[0][0][1])
        # The payload has 9 stages; in_bed is included but counted under in_bed_seconds
        assert len(state.stages) >= 8  # at least the 8 watch stages + 1 in_bed


class TestHistoricalBulkUploadMerging:
    """Regression tests: consecutive payloads for the same night must produce
    a single merged DB record rather than one record per payload.

    Root cause: Apple sends one night's sleep as many small consecutive payloads
    (each ending where the next begins).  When uploaded hours after recording the
    synchronous stale-check fires on every payload (now - end_time >> 2 h),
    immediately finalizing each payload as its own separate session.

    The fix: finish_sleep() queries for an adjacent existing record and merges
    instead of always inserting.  Combined with the per-user Redis lock that
    serializes concurrent tasks, this guarantees one DB record per night
    regardless of how many payloads Apple sends.
    """

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.sdk.sleep_service.event_record_service")
    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_second_payload_merges_with_adjacent_db_record(
        self,
        mock_redis_func: MagicMock,
        mock_event_service: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """When payload B arrives after payload A has already been finalized to the
        DB, finish_sleep should find the adjacent record, delete it and create a
        new merged record covering both sessions.

        Payload A: 23:00–01:00 (light + deep)
        Payload B: 01:00–06:00 (rem + light), chains directly onto A
        """

        user_id = str(uuid4())

        payload_b = {
            "provider": "apple",
            "sdkVersion": "1.0.0",
            "syncTimestamp": "2026-03-23T08:00:01Z",
            "data": {
                "records": [],
                "workouts": [],
                "sleep": [
                    {
                        "id": "B003",
                        "stage": "rem",
                        "startDate": "2026-03-23T01:00:00Z",
                        "endDate": "2026-03-23T02:30:00Z",
                        "source": {"device_type": "watch", "device_model": "Watch7,9"},
                    },
                    {
                        "id": "B004",
                        "stage": "light",
                        "startDate": "2026-03-23T02:30:00Z",
                        "endDate": "2026-03-23T06:00:00Z",
                        "source": {"device_type": "watch", "device_model": "Watch7,9"},
                    },
                ],
            },
        }

        # Simulate the adjacent record that payload A already wrote to the DB
        mock_adjacent = MagicMock()
        mock_adjacent.id = uuid4()
        mock_adjacent.data_source_id = uuid4()
        mock_adjacent.source_name = "unknown"  # the payload's writer: same-writer merge
        mock_adjacent.start_datetime = _dt("2026-03-22T23:00:00Z")
        mock_adjacent.end_datetime = _dt("2026-03-23T01:00:00Z")
        mock_adjacent.sleep_detail = MagicMock()
        mock_adjacent.sleep_detail.sleep_stages = [
            {"stage": "light", "start_time": "2026-03-22T23:00:00+00:00", "end_time": "2026-03-22T23:45:00+00:00"},
            {"stage": "deep", "start_time": "2026-03-22T23:45:00+00:00", "end_time": "2026-03-23T01:00:00+00:00"},
        ]

        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # No active Redis state for this user
        mock_redis_func.return_value = mock_redis

        mock_record = MagicMock()
        mock_record.id = uuid4()
        mock_event_service.create.return_value = mock_record
        # First call to find_adjacent returns the payload-A record; second call (if any) returns None
        mock_event_service.find_adjacent_sleep_record.return_value = mock_adjacent

        handle_sleep_data(db, SyncRequest.model_validate(payload_b), user_id)

        # find_adjacent_sleep_record must have been called to look for a matching record
        mock_event_service.find_adjacent_sleep_record.assert_called_once()

        # The old record must be deleted before the merged one is created
        mock_event_service.delete.assert_called_once_with(db, mock_adjacent.id)

        # A new (merged) record must be created
        mock_event_service.create.assert_called_once()
        created_record: EventRecordCreate = mock_event_service.create.call_args[0][1]

        # Merged window covers both A and B
        assert created_record.start_datetime <= _dt("2026-03-22T23:00:00Z")
        assert created_record.end_datetime >= _dt("2026-03-23T06:00:00Z")

        # Detail must contain stages from both payloads
        mock_event_service.create_detail.assert_called_once()
        created_detail: EventRecordDetailCreate = mock_event_service.create_detail.call_args[0][1]
        assert created_detail.sleep_stages is not None
        stage_types = {s.stage for s in created_detail.sleep_stages}
        assert SleepStageType.LIGHT in stage_types
        assert SleepStageType.DEEP in stage_types
        assert SleepStageType.REM in stage_types


class TestReadyToFinish:
    """FORK (port of upstream #1892): a session closes after the end gap, and one holding Apple
    Watch sleep also once uploads to it go quiet for ``sleep_upload_quiet_minutes``.

    The fork keeps one open session per user (upstream keys one per writer app, #1876), so these
    cases are upstream's tests adapted to a single state saved with ``redis.set``.
    """

    NOW = _dt("2026-04-18T09:00:00Z")
    WATCH = {"appId": "com.apple.health.FA11388E", "deviceType": "watch", "deviceModel": "Watch7,1"}
    IPHONE = {"appId": "com.apple.health.81F2A6D0", "deviceType": "phone", "deviceModel": "iPhone15,2"}
    OURA = {"appId": "com.ouraring.oura", "deviceType": "phone", "deviceModel": "iPhone15,2"}
    THIRD_PARTY_ON_WATCH = {"appId": "com.tantsissa.AutoSleep", "deviceType": "watch", "deviceModel": "Watch7,1"}

    def _state(
        self, ended_ago: int, received_ago: int | None, watch_sleep: bool = True, now: datetime = NOW
    ) -> SleepState:
        end = now - timedelta(minutes=ended_ago)
        return SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            provider="apple",
            start_time=end - timedelta(hours=7),
            end_time=end,
            last_start_timestamp=end - timedelta(minutes=10),
            last_end_timestamp=end,
            last_received_at=None if received_ago is None else now - timedelta(minutes=received_ago),
            has_apple_watch_sleep=watch_sleep,
        )

    def _payload(self, samples: list[tuple[str, str, str, dict]]) -> SyncRequest:
        """One upload of (stage, start, end, source) samples."""
        return SyncRequest.model_validate(
            {
                "provider": "apple",
                "sdkVersion": "0.13.0",
                "syncTimestamp": "2026-04-18T08:00:00Z",
                "data": {
                    "sleep": [
                        {"id": str(uuid4()), "stage": stage, "startDate": start, "endDate": end, "source": source}
                        for stage, start, end, source in samples
                    ]
                },
            }
        )

    def _upload(
        self,
        db: Session,
        mock_redis_func: MagicMock,
        samples: list[tuple[str, str, str, dict]],
        existing: SleepState | None = None,
    ) -> SleepState | None:
        """Handle one upload and return the state it saved, or None when nothing was saved."""
        mock_redis_func.return_value.get.return_value = existing.model_dump_json() if existing else None
        with (
            patch("app.services.sdk.sleep_service.finish_sleep"),
            patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps"),
        ):
            handle_sleep_data(db, self._payload(samples), str(uuid4()))
        calls = mock_redis_func.return_value.set.call_args_list
        return SleepState.model_validate_json(calls[-1].args[1]) if calls else None

    def test_any_session_closes_after_the_end_gap(self) -> None:
        assert is_ready_to_finish(self._state(ended_ago=120, received_ago=0, watch_sleep=False), self.NOW)

    def test_an_apple_watch_session_closes_after_a_quiet_period(self) -> None:
        assert is_ready_to_finish(self._state(ended_ago=40, received_ago=15), self.NOW)

    def test_an_apple_watch_session_stays_open_while_uploads_arrive(self) -> None:
        assert not is_ready_to_finish(self._state(ended_ago=40, received_ago=14), self.NOW)

    def test_a_session_without_apple_watch_sleep_waits_for_the_end_gap(self) -> None:
        assert not is_ready_to_finish(self._state(ended_ago=40, received_ago=30, watch_sleep=False), self.NOW)

    def test_a_state_without_an_upload_time_waits_for_the_end_gap(self) -> None:
        """A state saved before this change has no upload time: it keeps the old 2-hour rule."""
        assert not is_ready_to_finish(self._state(ended_ago=40, received_ago=None), self.NOW)

    def test_a_state_saved_before_the_change_still_loads(self) -> None:
        legacy = self._state(ended_ago=40, received_ago=None).model_dump(mode="json")
        del legacy["last_received_at"], legacy["has_apple_watch_sleep"]

        state = SleepState.model_validate(legacy)

        assert state.last_received_at is None
        assert not state.has_apple_watch_sleep

    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_an_upload_records_when_it_arrived(self, mock_redis_func: MagicMock, db: Session) -> None:
        before = datetime.now(timezone.utc)

        saved = self._upload(
            db, mock_redis_func, [("light", "2026-04-18T01:00:00Z", "2026-04-18T02:00:00Z", self.WATCH)]
        )

        assert saved is not None
        assert saved.last_received_at is not None
        assert saved.last_received_at >= before

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.sdk.sleep_service.finish_sleep")
    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_a_fresh_watch_upload_stays_open(
        self, mock_redis_func: MagicMock, mock_finish_sleep: MagicMock, mock_finalize: MagicMock, db: Session
    ) -> None:
        """The quiet period starts at the upload, so the night is not closed in the same call."""
        mock_redis_func.return_value.get.return_value = None
        woke_up = datetime.now(timezone.utc) - timedelta(minutes=30)
        sample = ("light", (woke_up - timedelta(hours=1)).isoformat(), woke_up.isoformat(), self.WATCH)

        handle_sleep_data(db, self._payload([sample]), str(uuid4()))

        mock_finish_sleep.assert_not_called()

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.sdk.sleep_service.finish_sleep")
    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_a_new_upload_restarts_the_quiet_period(
        self, mock_redis_func: MagicMock, mock_finish_sleep: MagicMock, mock_finalize: MagicMock, db: Session
    ) -> None:
        """A session already quiet for 20 minutes is not closed by the upload that extends it."""
        now = datetime.now(timezone.utc)
        existing = self._state(ended_ago=40, received_ago=20, now=now)
        mock_redis_func.return_value.get.return_value = existing.model_dump_json()
        sample = (
            "awake",
            existing.end_time.isoformat(),
            (existing.end_time + timedelta(minutes=5)).isoformat(),
            self.WATCH,
        )

        handle_sleep_data(db, self._payload([sample]), str(uuid4()))

        mock_finish_sleep.assert_not_called()
        saved = SleepState.model_validate_json(mock_redis_func.return_value.set.call_args.args[1])
        assert saved.last_received_at is not None
        assert saved.last_received_at > existing.last_received_at  # type: ignore[operator]

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (WATCH, True),
            ({**WATCH, "deviceType": "WATCH"}, True),
            (IPHONE, False),
            (OURA, False),
            (THIRD_PARTY_ON_WATCH, False),
            ({"appId": "com.apple.health.FA11388E", "deviceModel": "Watch7,1"}, False),
            ({"deviceType": "watch", "deviceModel": "Watch7,1"}, False),
            ({"bundleIdentifier": "com.apple.health.FA11388E", "deviceType": "watch", "deviceModel": "Watch7,1"}, True),
        ],
        ids=[
            "apple-watch",
            "apple-watch-upper-case",
            "apple-iphone",
            "third-party-on-phone",
            "third-party-on-watch",
            "apple-source-no-device-type",
            "watch-without-app-id",
            "apple-watch-bundle-identifier-only",
        ],
    )
    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_only_apple_watch_sleep_marks_the_session(
        self, mock_redis_func: MagicMock, source: dict, expected: bool, db: Session
    ) -> None:
        """Both conditions hold: Apple's own HealthKit source AND a reported watch, as upstream."""
        saved = self._upload(db, mock_redis_func, [("light", "2026-04-18T01:00:00Z", "2026-04-18T02:00:00Z", source)])

        assert saved is not None
        assert saved.has_apple_watch_sleep is expected

    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_watch_sleep_counts_in_a_session_the_iphone_opened(self, mock_redis_func: MagicMock, db: Session) -> None:
        saved = self._upload(
            db,
            mock_redis_func,
            [
                ("in_bed", "2026-04-18T00:30:00Z", "2026-04-18T07:00:00Z", self.IPHONE),
                ("light", "2026-04-18T01:00:00Z", "2026-04-18T02:00:00Z", self.WATCH),
            ],
        )

        assert saved is not None
        assert saved.has_apple_watch_sleep

    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_the_watch_mark_survives_a_later_iphone_upload(self, mock_redis_func: MagicMock, db: Session) -> None:
        existing = self._state(ended_ago=40, received_ago=20, now=datetime.now(timezone.utc))
        sample = (
            "in_bed",
            existing.end_time.isoformat(),
            (existing.end_time + timedelta(minutes=5)).isoformat(),
            self.IPHONE,
        )

        saved = self._upload(db, mock_redis_func, [sample], existing=existing)

        assert saved is not None
        assert saved.has_apple_watch_sleep

    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_a_night_split_off_inside_an_upload_records_the_upload_time(
        self, mock_redis_func: MagicMock, db: Session
    ) -> None:
        """A sample more than the end gap away starts a new night; that new night is the one saved."""
        before = datetime.now(timezone.utc)
        existing = self._state(ended_ago=600, received_ago=500, watch_sleep=False, now=before)
        start = existing.end_time + timedelta(hours=4)

        saved = self._upload(
            db,
            mock_redis_func,
            [("light", start.isoformat(), (start + timedelta(hours=1)).isoformat(), self.WATCH)],
            existing,
        )

        assert saved is not None
        assert saved.uuid != existing.uuid
        assert saved.has_apple_watch_sleep
        assert saved.last_received_at is not None
        assert saved.last_received_at >= before

    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_an_upload_without_usable_sleep_keeps_the_upload_time(
        self, mock_redis_func: MagicMock, db: Session
    ) -> None:
        existing = self._state(ended_ago=40, received_ago=30, now=datetime.now(timezone.utc))

        saved = self._upload(
            db, mock_redis_func, [("napping", "2026-04-18T08:30:00Z", "2026-04-18T08:40:00Z", self.WATCH)], existing
        )

        assert saved is not None
        assert saved.last_received_at == existing.last_received_at

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.sdk.sleep_service.finish_sleep")
    @patch("app.services.sdk.sleep_service.get_redis_client")
    def test_an_upload_without_usable_sleep_closes_a_quiet_watch_session(
        self, mock_redis_func: MagicMock, mock_finish_sleep: MagicMock, mock_finalize: MagicMock, db: Session
    ) -> None:
        """The upload path applies the same rule as the periodic task, not only the end gap."""
        existing = self._state(ended_ago=40, received_ago=20, now=datetime.now(timezone.utc))
        mock_redis_func.return_value.get.return_value = existing.model_dump_json()

        handle_sleep_data(
            db,
            self._payload([("napping", "2026-04-18T08:30:00Z", "2026-04-18T08:40:00Z", self.WATCH)]),
            str(uuid4()),
        )

        mock_finish_sleep.assert_called_once()


class TestLatePieceFromAnotherWriter:
    """FORK (with the port of upstream #1892): a piece of an Apple Health night that arrives after
    the night was closed merges into it whichever writer recorded it, as it would have joined the
    one open session while the night was still open. Real database, no mocked service: the merge
    is a query, and a mocked seam would hide which records it can reach.

    Without this an Apple Watch night closed after the 15-minute quiet period, followed by a later
    AutoSleep or Oura copy (or a later Watch piece in a night the iPhone opened), was stored as a
    second overlapping record of the same provider.
    """

    WATCH = ("Jan's Apple Watch", "Watch7,1")
    IPHONE = ("Jan's iPhone", "iPhone15,2")
    AUTOSLEEP = ("AutoSleep", "iPhone15,2")

    def _state(
        self,
        provider: str,
        writer: tuple[str, str],
        start: str,
        end: str,
        stage: SleepStageType = SleepStageType.LIGHT,
    ) -> SleepState:
        start_dt, end_dt = _dt(start), _dt(end)
        return SleepState(
            uuid=str(uuid4()),
            source_name=writer[0],
            device_model=writer[1],
            provider=provider,
            start_time=start_dt,
            end_time=end_dt,
            last_start_timestamp=start_dt,
            last_end_timestamp=end_dt,
            stages=[SleepStateStage(stage=stage, start_time=start_dt, end_time=end_dt)],
        )

    def _nights(self, db: Session, user_id: str) -> list[tuple[str, str | None, datetime, datetime]]:
        rows = (
            db.query(
                EventRecord.source_name, DataSource.device_model, EventRecord.start_datetime, EventRecord.end_datetime
            )
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(DataSource.user_id == UUID(user_id), EventRecord.category == "sleep")
            .order_by(EventRecord.start_datetime)
            .all()
        )
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def _writer_of_data_source(
        self, db: Session, nights: list[tuple[str, str | None, datetime, datetime]]
    ) -> str | None:
        """``DataSource.source`` of the one stored night: the row the merged record landed on."""
        row = (
            db.query(DataSource.source)
            .join(EventRecord, EventRecord.data_source_id == DataSource.id)
            .filter(EventRecord.start_datetime == nights[0][2], EventRecord.end_datetime == nights[0][3])
            .one()
        )
        return row[0]

    def _two_pieces(
        self, db: Session, provider: str, first: tuple[str, str], later: tuple[str, str], overlap: bool = True
    ) -> list[tuple[str, str | None, datetime, datetime]]:
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        with patch("app.services.sdk.sleep_service.delete_sleep_state"):
            finish_sleep(db, user_id, self._state(provider, first, "2026-04-17T23:00:00Z", "2026-04-18T06:00:00Z"))
            later_start = "2026-04-17T23:10:00Z" if overlap else "2026-04-18T06:30:00Z"
            finish_sleep(db, user_id, self._state(provider, later, later_start, "2026-04-18T06:45:00Z"))
        return self._nights(db, user_id)

    def test_a_later_copy_from_another_app_merges_into_the_watch_night(self, db: Session) -> None:
        nights = self._two_pieces(db, "apple", self.WATCH, self.AUTOSLEEP)

        assert len(nights) == 1
        assert nights[0][2] == _dt("2026-04-17T23:00:00Z")
        assert nights[0][3] == _dt("2026-04-18T06:45:00Z")

    def test_a_later_watch_piece_merges_into_a_night_the_iphone_opened(self, db: Session) -> None:
        nights = self._two_pieces(db, "apple", self.IPHONE, self.WATCH, overlap=False)

        assert len(nights) == 1

    def test_the_merged_night_keeps_the_stored_nights_writer_and_device(self, db: Session) -> None:
        """As the open session did: the night is named for the writer that opened it."""
        nights = self._two_pieces(db, "apple", self.WATCH, self.AUTOSLEEP)

        assert len(nights) == 1
        assert (nights[0][0], nights[0][1]) == self.WATCH
        assert self._writer_of_data_source(db, nights) == self.WATCH[0]

    def test_the_same_writer_still_merges(self, db: Session) -> None:
        nights = self._two_pieces(db, "apple", self.WATCH, self.WATCH, overlap=False)

        assert len(nights) == 1
        assert (nights[0][0], nights[0][1]) == self.WATCH

    def test_another_sdk_provider_keeps_one_record_per_writer(self, db: Session) -> None:
        """Only Apple Health has the 15-minute close; Health Connect keeps upstream's merge."""
        nights = self._two_pieces(db, "health_connect", ("Fitbit", "Pixel Watch"), ("Sleep as Android", "Pixel 8"))

        assert len(nights) == 2

    def test_a_night_more_than_the_end_gap_away_stays_its_own_record(self, db: Session) -> None:
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        with patch("app.services.sdk.sleep_service.delete_sleep_state"):
            finish_sleep(db, user_id, self._state("apple", self.WATCH, "2026-04-17T23:00:00Z", "2026-04-18T06:00:00Z"))
            finish_sleep(
                db, user_id, self._state("apple", self.AUTOSLEEP, "2026-04-18T13:00:00Z", "2026-04-18T14:00:00Z")
            )

        assert len(self._nights(db, user_id)) == 2

    def test_another_providers_night_is_never_merged_in(self, db: Session) -> None:
        """With the writer filter off for Apple Health, the provider filter alone keeps a night
        from another provider (Polar, read from its own API) out of the merge."""
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        with patch("app.services.sdk.sleep_service.delete_sleep_state"):
            finish_sleep(
                db,
                user_id,
                self._state("polar", ("Polar", "Vantage V3"), "2026-04-17T23:00:00Z", "2026-04-18T06:00:00Z"),
            )
            finish_sleep(db, user_id, self._state("apple", self.WATCH, "2026-04-17T23:10:00Z", "2026-04-18T06:45:00Z"))

        assert sorted(n[0] for n in self._nights(db, user_id)) == ["Jan's Apple Watch", "Polar"]

    def test_the_merged_night_counts_overlapping_time_once(self, db: Session) -> None:
        """Watch 23:00-06:00 and AutoSleep 23:10-06:45 are one night of 7 h 45 min, not 14 h 35 min."""
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        with patch("app.services.sdk.sleep_service.delete_sleep_state"):
            finish_sleep(db, user_id, self._state("apple", self.WATCH, "2026-04-17T23:00:00Z", "2026-04-18T06:00:00Z"))
            finish_sleep(
                db, user_id, self._state("apple", self.AUTOSLEEP, "2026-04-17T23:10:00Z", "2026-04-18T06:45:00Z")
            )

        totals = (
            db.query(SleepDetails.sleep_total_duration_minutes)
            .join(EventRecord, SleepDetails.record_id == EventRecord.id)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(DataSource.user_id == UUID(user_id))
            .all()
        )
        assert [t[0] for t in totals] == [465]


Piece = tuple[tuple[str, str], str, str, SleepStageType]

AUTOSLEEP = ("AutoSleep", "iPhone15,2")
WATCH = ("Jan's Apple Watch", "Watch7,1")
IPHONE = ("Jan's iPhone", "iPhone15,2")


def _sdk_state(provider: str, piece: Piece) -> SleepState:
    (name, device), start, end, stage = piece
    start_dt, end_dt = _dt(start), _dt(end)
    return SleepState(
        uuid=str(uuid4()),
        source_name=name,
        device_model=device,
        provider=provider,
        start_time=start_dt,
        end_time=end_dt,
        last_start_timestamp=start_dt,
        last_end_timestamp=end_dt,
        stages=[SleepStateStage(stage=stage, start_time=start_dt, end_time=end_dt)],
    )


def _store(db: Session, pieces: list[Piece], provider: str = "apple") -> list[tuple[int, int]]:
    """Close each piece through ``finish_sleep`` in order; (total sleep, time in bed) per stored night."""
    user_id = str(uuid4())
    UserFactory(id=UUID(user_id))
    with patch("app.services.sdk.sleep_service.delete_sleep_state"):
        for piece in pieces:
            finish_sleep(db, user_id, _sdk_state(provider, piece))
    rows = (
        db.query(SleepDetails.sleep_total_duration_minutes, SleepDetails.sleep_time_in_bed_minutes)
        .join(EventRecord, SleepDetails.record_id == EventRecord.id)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_id == UUID(user_id))
        .order_by(EventRecord.start_datetime)
        .all()
    )
    return [(r[0], r[1]) for r in rows]


class TestCrossWriterMergeKeepsGenericSleep:
    """FORK (reviews of #56): ``_calculate_final_metrics`` drops every generic ``sleeping`` stage once
    any light/deep/REM stage is present. Records from two writers used to stay separate, so a
    cross-writer merge keeps the generic time that no kept stage covers, counted once. A same-writer
    merge keeps the existing rule."""

    def test_generic_sleep_next_to_another_writers_detailed_piece_is_kept(self, db: Session) -> None:
        """Generic 00:00-07:00, then another writer's light 07:00-07:45: 465 min, not 45."""
        stored = _store(
            db,
            [
                (AUTOSLEEP, "2026-04-18T00:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING),
                (WATCH, "2026-04-18T07:00:00Z", "2026-04-18T07:45:00Z", SleepStageType.LIGHT),
            ],
        )

        assert [total for total, _ in stored] == [465]

    def test_an_edge_overlap_keeps_the_rest_of_the_generic_span(self, db: Session) -> None:
        """Generic 00:00-07:00, light 06:55-07:45: the 5 overlapping minutes count once (465)."""
        stored = _store(
            db,
            [
                (AUTOSLEEP, "2026-04-18T00:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING),
                (WATCH, "2026-04-18T06:55:00Z", "2026-04-18T07:45:00Z", SleepStageType.LIGHT),
            ],
        )

        assert [total for total, _ in stored] == [465]

    def test_an_awake_edge_keeps_the_rest_of_the_generic_span(self, db: Session) -> None:
        """Generic 00:00-07:00, then awake 06:55-07:05 and light 07:05-07:45: 415 + 40 = 455."""
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        later = _sdk_state("apple", (WATCH, "2026-04-18T06:55:00Z", "2026-04-18T07:05:00Z", SleepStageType.AWAKE))
        later.stages.append(
            SleepStateStage(
                stage=SleepStageType.LIGHT, start_time=_dt("2026-04-18T07:05:00Z"), end_time=_dt("2026-04-18T07:45:00Z")
            )
        )
        later.end_time = _dt("2026-04-18T07:45:00Z")
        with patch("app.services.sdk.sleep_service.delete_sleep_state"):
            finish_sleep(
                db,
                user_id,
                _sdk_state(
                    "apple", (AUTOSLEEP, "2026-04-18T00:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING)
                ),
            )
            finish_sleep(db, user_id, later)
        totals = (
            db.query(SleepDetails.sleep_total_duration_minutes)
            .join(EventRecord, SleepDetails.record_id == EventRecord.id)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(DataSource.user_id == UUID(user_id))
            .all()
        )

        assert [t[0] for t in totals] == [455]

    def test_generic_sleep_outside_a_partial_detailed_piece_is_kept(self, db: Session) -> None:
        """Generic 23:00-07:00 around a detailed piece 23:30-01:00: 480 min, counted once."""
        stored = _store(
            db,
            [
                (AUTOSLEEP, "2026-04-17T23:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING),
                (WATCH, "2026-04-17T23:30:00Z", "2026-04-18T01:00:00Z", SleepStageType.DEEP),
            ],
        )

        assert [total for total, _ in stored] == [480]

    def test_detailed_sleep_stored_first_keeps_a_later_generic_piece(self, db: Session) -> None:
        """Watch light 23:00-06:00, then AutoSleep generic 23:00-07:00: 480 min."""
        stored = _store(
            db,
            [
                (WATCH, "2026-04-17T23:00:00Z", "2026-04-18T06:00:00Z", SleepStageType.LIGHT),
                (AUTOSLEEP, "2026-04-17T23:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING),
            ],
        )

        assert [total for total, _ in stored] == [480]

    def test_an_iphone_in_bed_record_stored_first_counts_its_margins_as_sleep(self, db: Session) -> None:
        """Known limit: an in-bed-only record is stored as generic sleep, so in bed 01:00-07:30 stored
        before Watch light 01:30-07:00 counts 390 min of sleep and 390 in bed (one live session: 330
        and 390). release/0.9.0-syn stores the two records, 390 and 330."""
        stored = _store(
            db,
            [
                (IPHONE, "2026-04-18T01:00:00Z", "2026-04-18T07:30:00Z", SleepStageType.IN_BED),
                (WATCH, "2026-04-18T01:30:00Z", "2026-04-18T07:00:00Z", SleepStageType.LIGHT),
            ],
        )

        assert stored == [(390, 390)]

    def test_a_same_writer_merge_keeps_the_existing_rule(self, db: Session) -> None:
        """Unchanged: one writer's generic wrapper is dropped once that writer sends detailed stages."""
        stored = _store(
            db,
            [
                (WATCH, "2026-04-18T00:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING),
                (WATCH, "2026-04-18T07:00:00Z", "2026-04-18T07:45:00Z", SleepStageType.LIGHT),
            ],
        )

        assert [total for total, _ in stored] == [45]

    def test_overlapping_generic_stages_are_counted_once(self, db: Session) -> None:
        """Generic 00:00-07:00 and 06:00-07:00 in one upload are 420 min, plus light 07:00-07:45: 465."""
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        later = _sdk_state(
            "apple", (AUTOSLEEP, "2026-04-18T00:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING)
        )
        later.stages.append(
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-04-18T06:00:00Z"),
                end_time=_dt("2026-04-18T07:00:00Z"),
            )
        )
        with patch("app.services.sdk.sleep_service.delete_sleep_state"):
            finish_sleep(
                db,
                user_id,
                _sdk_state("apple", (WATCH, "2026-04-18T07:00:00Z", "2026-04-18T07:45:00Z", SleepStageType.LIGHT)),
            )
            finish_sleep(db, user_id, later)
        totals = (
            db.query(SleepDetails.sleep_total_duration_minutes)
            .join(EventRecord, SleepDetails.record_id == EventRecord.id)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(DataSource.user_id == UUID(user_id))
            .all()
        )

        assert [t[0] for t in totals] == [465]


def _watch_night_with_wrapper(start: str, end: str, stages: list[tuple[str, str, SleepStageType]]) -> SleepState:
    """A Watch piece that sends a generic ``sleeping`` wrapper around its own detailed stages."""
    state = _sdk_state("apple", (WATCH, start, end, SleepStageType.SLEEPING))
    for s_start, s_end, stage in stages:
        state.stages.append(SleepStateStage(stage=stage, start_time=_dt(s_start), end_time=_dt(s_end)))
    return state


def _totals(db: Session, user_id: str) -> list[int]:
    rows = (
        db.query(SleepDetails.sleep_total_duration_minutes)
        .join(EventRecord, SleepDetails.record_id == EventRecord.id)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_id == UUID(user_id))
        .all()
    )
    return [r[0] for r in rows]


class TestALatePiecesOwnWrapperIsNotSleep:
    """FORK (review of b443dbb8): a late piece that sends a generic wrapper around its own detailed
    stages keeps the existing rule for that wrapper; only another source's generic sleep is kept."""

    WATCH_STAGES = [
        ("2026-04-17T23:30:00Z", "2026-04-18T03:00:00Z", SleepStageType.LIGHT),
        ("2026-04-18T03:30:00Z", "2026-04-18T06:30:00Z", SleepStageType.DEEP),
    ]

    def test_a_cross_writer_merge_does_not_count_the_late_pieces_wrapper(self, db: Session) -> None:
        """AutoSleep generic 22:00-23:00, then a Watch wrapper 23:00-07:00 around 390 min of stages: 450."""
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        with patch("app.services.sdk.sleep_service.delete_sleep_state"):
            finish_sleep(
                db,
                user_id,
                _sdk_state(
                    "apple", (AUTOSLEEP, "2026-04-17T22:00:00Z", "2026-04-17T23:00:00Z", SleepStageType.SLEEPING)
                ),
            )
            finish_sleep(
                db,
                user_id,
                _watch_night_with_wrapper("2026-04-17T23:00:00Z", "2026-04-18T07:00:00Z", self.WATCH_STAGES),
            )

        assert _totals(db, user_id) == [450]

    def test_a_later_wrapper_on_a_record_holding_both_kinds_is_not_sleep(self, db: Session) -> None:
        """A record that kept AutoSleep's 00:00-07:00 beside Watch light 07:00-07:45 (465), then a Watch
        wrapper 07:45-09:00 around deep 08:00-08:30: 495, not 540."""
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        with patch("app.services.sdk.sleep_service.delete_sleep_state"):
            finish_sleep(
                db,
                user_id,
                _sdk_state("apple", (WATCH, "2026-04-18T07:00:00Z", "2026-04-18T07:45:00Z", SleepStageType.LIGHT)),
            )
            finish_sleep(
                db,
                user_id,
                _sdk_state(
                    "apple", (AUTOSLEEP, "2026-04-18T00:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING)
                ),
            )
            finish_sleep(
                db,
                user_id,
                _watch_night_with_wrapper(
                    "2026-04-18T07:45:00Z",
                    "2026-04-18T09:00:00Z",
                    [("2026-04-18T08:00:00Z", "2026-04-18T08:30:00Z", SleepStageType.DEEP)],
                ),
            )

        assert _totals(db, user_id) == [495]


class TestMergeAcrossThreePieces:
    """FORK (reviews of #56): a record that kept another writer's generic sleep keeps it when a later
    piece comes from the writer the record is named for."""

    def test_a_later_piece_from_the_named_writer_keeps_restored_generic_sleep(self, db: Session) -> None:
        """Watch light 00:00-01:00, AutoSleep generic 00:00-07:00, Watch deep 07:00-07:30: 450 min."""
        stored = _store(
            db,
            [
                (WATCH, "2026-04-18T00:00:00Z", "2026-04-18T01:00:00Z", SleepStageType.LIGHT),
                (AUTOSLEEP, "2026-04-18T00:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING),
                (WATCH, "2026-04-18T07:00:00Z", "2026-04-18T07:30:00Z", SleepStageType.DEEP),
            ],
        )

        assert [total for total, _ in stored] == [450]

    def test_a_later_generic_piece_from_the_named_writer_keeps_everything(self, db: Session) -> None:
        """AutoSleep generic 00:00-07:00, Watch light 07:00-07:45, AutoSleep generic 07:45-08:00: 480 min."""
        stored = _store(
            db,
            [
                (AUTOSLEEP, "2026-04-18T00:00:00Z", "2026-04-18T07:00:00Z", SleepStageType.SLEEPING),
                (WATCH, "2026-04-18T07:00:00Z", "2026-04-18T07:45:00Z", SleepStageType.LIGHT),
                (AUTOSLEEP, "2026-04-18T07:45:00Z", "2026-04-18T08:00:00Z", SleepStageType.SLEEPING),
            ],
        )

        assert [total for total, _ in stored] == [480]


class TestOtherSdkProvidersMergeAsBefore:
    """FORK (Bogdan's reviews of #56): only an Apple Health merge keeps the stored night's writer and
    device, or keeps generic sleep beside detailed stages. Health Connect and Samsung merge as on
    release/0.9.0-syn."""

    def test_a_health_connect_merge_lands_on_the_late_pieces_device(self, db: Session) -> None:
        helper = TestLatePieceFromAnotherWriter()
        user_id = str(uuid4())
        UserFactory(id=UUID(user_id))
        writer = "com.sec.android.app.shealth"
        with patch("app.services.sdk.sleep_service.delete_sleep_state"):
            finish_sleep(
                db,
                user_id,
                helper._state("health_connect", (writer, "SM-R960"), "2026-04-17T23:00:00Z", "2026-04-18T06:00:00Z"),
            )
            finish_sleep(
                db,
                user_id,
                helper._state("health_connect", (writer, "SM-S918B"), "2026-04-18T06:10:00Z", "2026-04-18T06:40:00Z"),
            )

        nights = helper._nights(db, user_id)
        assert len(nights) == 1
        assert nights[0][1] == "SM-S918B"

    def test_a_source_less_health_connect_piece_keeps_the_existing_rule(self, db: Session) -> None:
        """A late piece with no source name has no writer filter for any provider, so it merges into
        another writer's night; Health Connect still drops the generic sleep there, as before (45)."""
        stored = _store(
            db,
            [
                (
                    ("Sleep as Android", "Pixel 8"),
                    "2026-04-18T00:00:00Z",
                    "2026-04-18T07:00:00Z",
                    SleepStageType.SLEEPING,
                ),
                (("unknown", "Pixel 8"), "2026-04-18T07:00:00Z", "2026-04-18T07:45:00Z", SleepStageType.LIGHT),
            ],
            provider="health_connect",
        )

        assert [total for total, _ in stored] == [45]
