"""Tests for the outgoing webhooks subsystem.

Covers:
- WebhookEventType enum (#717)
- webhook_emit helpers (#719, #721)
- emit_webhook_event Celery task
- outgoing_webhooks API router (#722-726)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from svix.api.errors.http_error import HttpError

from app.config import settings
from app.integrations.celery.tasks.emit_webhook_event_task import emit_webhook_event
from app.schemas.webhooks.event_types import EVENT_TYPE_DESCRIPTIONS, WebhookEventType
from app.services.outgoing_webhooks import pseudonyms
from app.services.outgoing_webhooks import svix as svix_service
from app.services.outgoing_webhooks.events import (
    SVIX_MAX_SAMPLES_PER_EVENT,
    _dispatch,
    on_connection_created,
    on_sleep_created,
    on_timeseries_batch_saved,
    on_workout_created,
)
from app.utils.security import create_access_token
from tests.factories import DeveloperFactory

# ---------------------------------------------------------------------------
# WebhookEventType enum
# ---------------------------------------------------------------------------


class TestWebhookEventTypes:
    def test_all_event_types_have_descriptions(self) -> None:
        for evt in WebhookEventType:
            assert evt in EVENT_TYPE_DESCRIPTIONS, f"Missing description for {evt}"

    def test_values_follow_convention(self) -> None:
        for evt in WebhookEventType:
            assert "." in evt.value, f"{evt} should follow resource.action convention"


# ---------------------------------------------------------------------------
# webhook_emit helpers (unit, no Celery/Redis required)
# ---------------------------------------------------------------------------


class TestWebhookEmit:
    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_on_workout_created_dispatches(self, mock_task: MagicMock) -> None:
        uid = uuid4()
        rid = uuid4()
        on_workout_created(
            record_id=rid,
            user_id=uid,
            provider="garmin",
            device="Forerunner 255",
            workout_type="RUNNING",
            start_time="2026-01-01T00:00:00",
            end_time="2026-01-01T01:00:00",
            zone_offset="+01:00",
            duration_seconds=3600,
            calories_kcal=450.0,
            distance_meters=10000.0,
            avg_heart_rate_bpm=155,
            max_heart_rate_bpm=178,
            elevation_gain_meters=120.0,
            avg_pace_sec_per_km=360,
        )
        mock_task.apply_async.assert_called_once()
        call = mock_task.apply_async.call_args
        assert call.kwargs["queue"] == "webhook_sync"
        args = call.kwargs["args"]
        assert args[0] == "workout.created"
        assert args[1]["data"]["source"]["provider"] == "garmin"
        assert args[1]["data"]["calories_kcal"] == 450.0
        assert args[1]["data"]["distance_meters"] == 10000.0
        assert args[1]["data"]["avg_heart_rate_bpm"] == 155

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_on_sleep_created_dispatches(self, mock_task: MagicMock) -> None:
        uid = uuid4()
        rid = uuid4()
        on_sleep_created(
            record_id=rid,
            user_id=uid,
            provider="oura",
            device="Oura Ring Gen3",
            start_time="2026-01-01T22:00:00",
            end_time="2026-01-02T06:00:00",
            zone_offset=None,
            duration_seconds=28800,
            efficiency_percent=85.0,
            stages={"deep_minutes": 90, "rem_minutes": 60, "light_minutes": 120, "awake_minutes": 10},
            is_nap=False,
        )
        mock_task.apply_async.assert_called_once()
        call = mock_task.apply_async.call_args
        assert call.kwargs["queue"] == "webhook_sync"
        args = call.kwargs["args"]
        assert args[0] == "sleep.created"
        assert args[1]["data"]["efficiency_percent"] == 85.0
        assert args[1]["data"]["stages"]["deep_minutes"] == 90

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_on_timeseries_batch_saved_dispatches(self, mock_task: MagicMock) -> None:
        uid = uuid4()
        samples = [
            {
                "timestamp": "2026-04-16T06:00:00+00:00",
                "zone_offset": "+00:00",
                "type": "heart_rate",
                "value": 62.0,
                "unit": "bpm",
                "source": {"provider": "garmin", "device": "Forerunner 255"},
            },
            {
                "timestamp": "2026-04-16T06:05:00+00:00",
                "zone_offset": "+00:00",
                "type": "heart_rate",
                "value": 65.0,
                "unit": "bpm",
                "source": {"provider": "garmin", "device": "Forerunner 255"},
            },
        ]
        on_timeseries_batch_saved(
            user_id=uid,
            provider="garmin",
            series_type="heart_rate",
            sample_count=2,
            start_time="2026-04-16T06:00:00+00:00",
            end_time="2026-04-16T06:05:00+00:00",
            samples=samples,
        )
        mock_task.delay.assert_called()
        assert mock_task.delay.call_count == 2
        calls = {c[0][0] for c in mock_task.delay.call_args_list}
        assert "heart_rate.created" in calls
        assert "series.heart_rate.created" in calls
        # validate payload on the group event
        group_call = next(c for c in mock_task.delay.call_args_list if c[0][0] == "heart_rate.created")
        args = group_call
        data = args[0][1]["data"]
        assert data["start_time"] == "2026-04-16T06:00:00+00:00"
        assert data["end_time"] == "2026-04-16T06:05:00+00:00"
        assert len(data["samples"]) == 2
        assert data["samples"][0]["value"] == 62.0
        assert data["samples"][1]["value"] == 65.0
        assert "chunk_index" not in data

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_on_timeseries_batch_saved_without_samples(self, mock_task: MagicMock) -> None:
        """Backward-compatible call without samples still dispatches correctly."""
        uid = uuid4()
        on_timeseries_batch_saved(
            user_id=uid,
            provider="garmin",
            series_type="heart_rate",
            sample_count=100,
        )
        assert mock_task.delay.call_count == 2
        calls = {c[0][0] for c in mock_task.delay.call_args_list}
        assert "heart_rate.created" in calls
        assert "series.heart_rate.created" in calls
        group_call = next(c for c in mock_task.delay.call_args_list if c[0][0] == "heart_rate.created")
        data = group_call[0][1]["data"]
        assert data["samples"] == []
        assert data["sample_count"] == 100

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_on_timeseries_batch_saved_chunks_large_payload(self, mock_task: MagicMock) -> None:
        """Batches exceeding SVIX_MAX_SAMPLES_PER_EVENT are split into chunk events."""
        uid = uuid4()
        large_samples = [
            {
                "timestamp": f"2026-04-16T{i // 3600:02d}:{(i % 3600) // 60:02d}:{i % 60:02d}+00:00",
                "zone_offset": "+00:00",
                "type": "heart_rate",
                "value": float(60 + i % 40),
                "unit": "bpm",
                "source": {"provider": "garmin", "device": None},
            }
            for i in range(SVIX_MAX_SAMPLES_PER_EVENT + 10)
        ]
        on_timeseries_batch_saved(
            user_id=uid,
            provider="garmin",
            series_type="heart_rate",
            sample_count=len(large_samples),
            start_time=large_samples[0]["timestamp"],
            end_time=large_samples[-1]["timestamp"],
            samples=large_samples,
        )
        # 2 chunks × 2 event types (group + granular) = 4 calls
        assert mock_task.delay.call_count == 4
        group_calls = [c for c in mock_task.delay.call_args_list if c[0][0] == "heart_rate.created"]
        assert len(group_calls) == 2
        first_data = group_calls[0][0][1]["data"]
        second_data = group_calls[1][0][1]["data"]
        assert first_data["chunk_index"] == 0
        assert first_data["total_chunks"] == 2
        assert second_data["chunk_index"] == 1
        assert second_data["total_chunks"] == 2
        assert len(first_data["samples"]) == SVIX_MAX_SAMPLES_PER_EVENT
        assert len(second_data["samples"]) == 10
        # sample_count reflects the full batch in every chunk
        assert first_data["sample_count"] == len(large_samples)
        assert second_data["sample_count"] == len(large_samples)

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_on_timeseries_skips_unmapped_series_type(self, mock_task: MagicMock) -> None:
        uid = uuid4()
        on_timeseries_batch_saved(
            user_id=uid,
            provider="polar",
            series_type="unknown_type_xyz",
            sample_count=5,
        )
        mock_task.delay.assert_not_called()

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_on_connection_created_dispatches(self, mock_task: MagicMock) -> None:
        uid = uuid4()
        cid = uuid4()
        on_connection_created(
            user_id=uid,
            provider="garmin",
            connection_id=cid,
            connected_at="2026-01-01T12:00:00+00:00",
        )
        mock_task.delay.assert_called_once()
        args = mock_task.delay.call_args
        assert args[0][0] == "connection.created"
        assert args[0][1]["data"]["provider"] == "garmin"
        assert args[0][1]["data"]["connection_id"] == str(cid)

    def test_dispatch_swallows_broker_error(self) -> None:
        """_dispatch silently drops the event when Celery is unreachable."""

        with patch(
            "app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event",
        ) as mock_task:
            # workout.created is a priority event → routed via apply_async
            mock_task.apply_async.side_effect = ConnectionError("Redis not available")
            # Should NOT raise
            _dispatch("workout.created", {"type": "workout.created", "data": {}})


# ---------------------------------------------------------------------------
# Svix payload retention
# ---------------------------------------------------------------------------


class TestSvixPayloadRetention:
    def test_send_sets_payload_retention_period(self) -> None:
        """Every emitted message must carry an explicit retention window."""
        mock_client = MagicMock()
        with patch.object(svix_service, "_client", mock_client):
            svix_service.send("workout.created", str(uuid4()), {"data": {}})

        message_in = mock_client.message.create.call_args[0][1]
        assert message_in.payload_retention_period == settings.svix_payload_retention_days


# ---------------------------------------------------------------------------
# has_endpoints: which lookup failures may skip a developer
# ---------------------------------------------------------------------------


class TestHasEndpoints:
    """Only a lookup that PROVES no endpoint exists may skip the developer.

    A skip acknowledges the Celery task with nothing sent and no retry, so a lookup that
    merely failed must fall through to ``send`` — which owns the delivery-failure contract —
    rather than turning a transient error into a silently dropped event.
    """

    @staticmethod
    def _client_listing(result: object) -> MagicMock:
        client = MagicMock()
        if isinstance(result, BaseException):
            client.endpoint.list.side_effect = result
        else:
            client.endpoint.list.return_value = result
        return client

    def test_an_application_with_endpoints_has_endpoints(self) -> None:
        client = self._client_listing(MagicMock(data=[MagicMock()]))
        with patch.object(svix_service, "_client", client):
            assert svix_service.has_endpoints(str(uuid4())) is True

    def test_an_application_with_no_endpoints_is_skipped(self) -> None:
        client = self._client_listing(MagicMock(data=[]))
        with patch.object(svix_service, "_client", client):
            assert svix_service.has_endpoints(str(uuid4())) is False

    def test_an_application_that_was_never_created_is_skipped(self) -> None:
        client = self._client_listing(HttpError("not_found", "no app", 404))
        with patch.object(svix_service, "_client", client):
            assert svix_service.has_endpoints(str(uuid4())) is False

    def test_an_unreachable_svix_does_not_skip_the_developer(self) -> None:
        client = self._client_listing(httpx.ConnectError("connection refused"))
        with (
            patch.object(svix_service, "_client", client),
            patch.object(svix_service, "log_and_capture_error") as capture,
        ):
            assert svix_service.has_endpoints(str(uuid4())) is True
        # Swallowed, so it must still reach Sentry (backend/AGENTS.md): a recurring outage
        # during the lookup is otherwise visible only in the application log.
        capture.assert_called_once()

    def test_any_other_lookup_failure_does_not_skip_the_developer(self) -> None:
        client = self._client_listing(HttpError("server_error", "boom", 500))
        with (
            patch.object(svix_service, "_client", client),
            patch.object(svix_service, "log_and_capture_error") as capture,
        ):
            assert svix_service.has_endpoints(str(uuid4())) is True
        capture.assert_called_once()

    def test_a_transient_lookup_failure_still_sends_the_event(self) -> None:
        """End to end through the real emit task: the lookup fails to connect, Svix recovers,
        and the event must still be sent rather than counted as skipped."""
        dev = MagicMock(id=uuid4(), email="dev@test.com")
        client = MagicMock()
        client.endpoint.list.side_effect = httpx.ConnectError("blip")
        client.message.create.return_value = MagicMock(id="msg_1")
        with (
            patch.object(svix_service, "_client", client),
            patch("app.integrations.celery.tasks.emit_webhook_event_task.developer_service") as devs,
        ):
            devs.crud.get_all.return_value = [dev]
            result = emit_webhook_event("workout.created", {"type": "workout.created", "data": {}})

        assert result["skipped"] == 0
        assert result["sent"] == 1
        assert client.message.create.call_count == 1


# ---------------------------------------------------------------------------
# Svix event id hashing
# ---------------------------------------------------------------------------


class TestSvixEventIdHashing:
    # The readable form was e.g.
    # "timeseries.<user-uuid>.apple.heart_rate.2026-09-12T12_37_09_00_00.<...>.series.heart_rate.created"
    RAW = (
        "timeseries.11de240a-4e95-4eed-ae00-983f34dcbd3b.apple.heart_rate"
        ".2026-09-12T12_37_09_00_00.2026-09-12T12_57_13_00_00.series.heart_rate.created"
    )

    def test_digest_carries_no_identifiers(self) -> None:
        digest = pseudonyms.hash_event_id(self.RAW)
        assert digest is not None
        for leaked in ("11de240a", "apple", "heart_rate", "2026-09-12", "timeseries"):
            assert leaked not in digest

    def test_send_hashes_the_idempotency_key(self) -> None:
        mock_client = MagicMock()
        with patch.object(svix_service, "_client", mock_client):
            svix_service.send("workout.created", str(uuid4()), {"data": {}}, idempotency_key=self.RAW)

        message_in = mock_client.message.create.call_args[0][1]
        assert message_in.event_id == pseudonyms.hash_event_id(self.RAW)
        assert self.RAW not in (message_in.event_id or "")


# ---------------------------------------------------------------------------
# User channels: Svix never receives a readable user id
# ---------------------------------------------------------------------------


class TestUserChannelsReachSvixAsPseudonyms:
    """``message.channels`` persists on the Svix message row with no expiry, beside ``uid``. The
    event-id hash alone left the user id readable there on every message."""

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_an_emitted_event_carries_the_pseudonymous_channel(self, mock_task: MagicMock) -> None:
        uid = uuid4()
        on_connection_created(
            user_id=uid,
            provider="garmin",
            connection_id=uuid4(),
            connected_at="2026-01-01T12:00:00+00:00",
        )
        channels = mock_task.delay.call_args.kwargs["channels"]
        assert channels == [pseudonyms.user_channel(uid)]
        assert str(uid) not in repr(channels)

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_a_fast_lane_event_carries_the_pseudonymous_channel(self, mock_task: MagicMock) -> None:
        uid = uuid4()
        on_workout_created(
            record_id=uuid4(),
            user_id=uid,
            provider="garmin",
            device="Forerunner 255",
            workout_type="RUNNING",
            start_time="2026-01-01T00:00:00",
            end_time="2026-01-01T01:00:00",
            zone_offset="+01:00",
            duration_seconds=3600,
            calories_kcal=450.0,
            distance_meters=10000.0,
            avg_heart_rate_bpm=155,
            max_heart_rate_bpm=178,
            elevation_gain_meters=120.0,
            avg_pace_sec_per_km=360,
        )
        channels = mock_task.apply_async.call_args.kwargs["kwargs"]["channels"]
        assert channels == [pseudonyms.user_channel(uid)]

    def test_no_module_builds_a_readable_user_channel(self) -> None:
        """Every user channel must come from ``pseudonyms.user_channel``. Scans the app package
        rather than a hand-written list of emitters, so a new emitter is covered by default."""
        app_root = Path(svix_service.__file__).resolve().parents[2]
        readable = re.compile(r"""f?["']user\.(\{|["'])""")
        offenders = [
            f"{path.relative_to(app_root)}:{n}"
            for path in sorted(app_root.rglob("*.py"))
            if path.name != "pseudonyms.py"
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if readable.search(line)
        ]
        assert offenders == []

    def test_create_and_update_endpoint_send_the_pseudonymous_channel(self) -> None:
        uid = uuid4()
        client = MagicMock()
        with patch.object(svix_service, "_client", client):
            svix_service.create_endpoint("app", "https://example.com/wh", user_id=uid)
            svix_service.patch_endpoint("app", "ep_1", user_id=uid)

        created = client.endpoint.create.call_args[0][1]
        patched = client.endpoint.patch.call_args[0][2]
        assert created.channels == [pseudonyms.user_channel(uid)]
        assert patched.channels == [pseudonyms.user_channel(uid)]

    @pytest.mark.parametrize("form", ["legacy", "pseudonymous"])
    def test_send_never_hands_svix_a_readable_user_channel(self, form: str) -> None:
        """The Svix boundary pseudonymises, not only the producers.

        A Celery job enqueued by the previous release (or by an old producer during a rolling
        deploy) still carries ``user.<uuid>``; the worker passes it straight to ``send``. Whatever
        form arrives, Svix must receive the pseudonymous channel — and an already-pseudonymous one
        must pass through unchanged, not be encrypted twice.
        """
        uid = uuid4()
        incoming = f"user.{uid}" if form == "legacy" else pseudonyms.user_channel(uid)
        client = MagicMock()
        with patch.object(svix_service, "_client", client):
            svix_service.send("workout.created", str(uuid4()), {"data": {}}, channels=[incoming, "project_123"])

        sent = client.message.create.call_args[0][1].channels
        assert sent == [pseudonyms.user_channel(uid), "project_123"]
        assert str(uid) not in repr(sent)

    def test_a_job_queued_by_the_previous_release_reaches_svix_pseudonymised(self) -> None:
        """End to end through the real emit task with the kwargs an old producer enqueued."""
        uid = uuid4()
        client = MagicMock()
        client.endpoint.list.return_value = MagicMock(data=[MagicMock()])
        client.message.create.return_value = MagicMock(id="msg_1")
        with (
            patch.object(svix_service, "_client", client),
            patch("app.integrations.celery.tasks.emit_webhook_event_task.developer_service") as devs,
        ):
            devs.crud.get_all.return_value = [MagicMock(id=uuid4(), email="dev@test.com")]
            emit_webhook_event(
                "workout.created",
                {"type": "workout.created", "data": {}},
                channels=[f"user.{uid}"],
                idempotency_key=f"workout.created.{uuid4()}",
            )

        sent = client.message.create.call_args[0][1].channels
        assert sent == [pseudonyms.user_channel(uid)]

    def test_an_endpoint_reports_its_user_filter(self) -> None:
        uid = uuid4()
        ep = MagicMock(channels=[pseudonyms.user_channel(uid)])
        assert svix_service.user_id_from_endpoint(ep) == uid


# ---------------------------------------------------------------------------
# emit_webhook_event Celery task (unit, Svix mocked)
# ---------------------------------------------------------------------------


class TestEmitWebhookEventTask:
    @patch("app.integrations.celery.tasks.emit_webhook_event_task.svix_service")
    @patch("app.integrations.celery.tasks.emit_webhook_event_task.developer_service")
    def test_sends_to_developers_with_endpoints(
        self,
        mock_dev_service: MagicMock,
        mock_svix: MagicMock,
    ) -> None:
        dev1 = MagicMock(id=uuid4(), email="dev1@test.com")
        dev2 = MagicMock(id=uuid4(), email="dev2@test.com")
        mock_dev_service.crud.get_all.return_value = [dev1, dev2]
        mock_svix.has_endpoints.return_value = True
        mock_svix.send.return_value = MagicMock(id="msg_123")

        result = emit_webhook_event(
            "workout.created",
            {"type": "workout.created", "data": {}},
        )

        assert result["sent"] == 2
        assert result["skipped"] == 0
        assert mock_svix.send.call_count == 2

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.svix_service")
    @patch("app.integrations.celery.tasks.emit_webhook_event_task.developer_service")
    def test_skips_developers_without_endpoints(
        self,
        mock_dev_service: MagicMock,
        mock_svix: MagicMock,
    ) -> None:
        with_ep = MagicMock(id=uuid4(), email="with@test.com")
        without_ep = MagicMock(id=uuid4(), email="without@test.com")
        mock_dev_service.crud.get_all.return_value = [with_ep, without_ep]
        mock_svix.has_endpoints.side_effect = lambda app_id: app_id == str(with_ep.id)
        mock_svix.send.return_value = MagicMock(id="msg_123")

        result = emit_webhook_event(
            "workout.created",
            {"type": "workout.created", "data": {}},
        )

        assert result["sent"] == 1
        assert result["skipped"] == 1
        assert mock_svix.send.call_count == 1
        assert mock_svix.send.call_args[0][1] == str(with_ep.id)
        # No Svix application is created for an account that never registered an endpoint.
        assert mock_svix.ensure_application.call_count == 0


# ---------------------------------------------------------------------------
# Outgoing webhooks API (#722-726)
# ---------------------------------------------------------------------------


class TestOutgoingWebhooksAPI:
    """Test the /api/v1/webhooks/* endpoints.

    Svix is fully mocked — these are integration tests for the FastAPI
    router, not the Svix server.
    """

    @pytest.fixture(autouse=True)
    def mock_svix(self) -> Any:
        """Mock svix_webhook_service to avoid needing a real Svix server."""
        with patch("app.api.routes.v1.outgoing_webhooks.svix_service") as m:
            m.is_enabled.return_value = True
            m.ensure_application.return_value = "app_uid_123"
            m.user_id_from_endpoint.return_value = None
            yield m

    def test_list_event_types(self, client: TestClient) -> None:
        resp = client.get("/api/v1/webhooks/event-types")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == len(WebhookEventType)
        names = {e["name"] for e in data}
        assert "workout.created" in names
        assert "sleep.created" in names

    def test_create_endpoint(
        self,
        client: TestClient,
        db: Session,
        mock_svix: MagicMock,
    ) -> None:
        developer = DeveloperFactory()

        token = create_access_token(developer.id)

        ep_out = MagicMock(id="ep_123", url="https://example.com/wh", description="test", filter_types=None)
        mock_svix.create_endpoint.return_value = ep_out

        resp = client.post(
            "/api/v1/webhooks/endpoints",
            json={"url": "https://example.com/wh", "description": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "ep_123"
        assert data["url"] == "https://example.com/wh"

    def test_list_endpoints(
        self,
        client: TestClient,
        db: Session,
        mock_svix: MagicMock,
    ) -> None:
        developer = DeveloperFactory()

        token = create_access_token(developer.id)

        list_resp = MagicMock()
        list_resp.data = [
            MagicMock(id="ep_1", url="https://a.com/wh", description="a", filter_types=None),
            MagicMock(id="ep_2", url="https://b.com/wh", description="b", filter_types=["workout.created"]),
        ]
        mock_svix.list_endpoints.return_value = list_resp

        resp = client.get(
            "/api/v1/webhooks/endpoints",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_delete_endpoint(
        self,
        client: TestClient,
        db: Session,
        mock_svix: MagicMock,
    ) -> None:
        developer = DeveloperFactory()

        token = create_access_token(developer.id)

        resp = client.delete(
            "/api/v1/webhooks/endpoints/ep_123",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204
        mock_svix.delete_endpoint.assert_called_once()

    def test_get_endpoint_secret(
        self,
        client: TestClient,
        db: Session,
        mock_svix: MagicMock,
    ) -> None:
        developer = DeveloperFactory()

        token = create_access_token(developer.id)

        mock_svix.get_endpoint_secret.return_value = "whsec_test_secret_key"

        resp = client.get(
            "/api/v1/webhooks/endpoints/ep_123/secret",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["key"] == "whsec_test_secret_key"

    def test_send_test_event(
        self,
        client: TestClient,
        db: Session,
        mock_svix: MagicMock,
    ) -> None:
        developer = DeveloperFactory()

        token = create_access_token(developer.id)

        mock_svix.send_test_message.return_value = MagicMock(id="msg_test_1")

        resp = client.post(
            "/api/v1/webhooks/endpoints/ep_123/test",
            headers={"Authorization": f"Bearer {token}"},
            json={"event_type": "workout.created"},
        )
        assert resp.status_code == 200
        assert "message_id" in resp.json()

    def test_messages_list_returns_readable_channels(
        self,
        client: TestClient,
        db: Session,
        mock_svix: MagicMock,
    ) -> None:
        """Svix holds the pseudonym; the developer's own API keeps returning ``user.<uuid>``."""
        developer = DeveloperFactory()
        token = create_access_token(developer.id)
        uid = uuid4()
        mock_svix.list_messages.return_value = MagicMock(
            data=[
                MagicMock(
                    id="msg_1",
                    event_type="workout.created",
                    event_id="d" * 64,
                    timestamp=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
                    channels=[pseudonyms.user_channel(uid)],
                    tags=None,
                )
            ],
            done=True,
            iterator=None,
            prev_iterator=None,
        )

        resp = client.get("/api/v1/webhooks/messages", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["data"][0]["channels"] == [f"user.{uid}"]

    def test_endpoints_require_auth(self, client: TestClient) -> None:
        resp = client.get("/api/v1/webhooks/endpoints")
        assert resp.status_code == 401

    def test_svix_disabled_returns_503(
        self,
        client: TestClient,
        db: Session,
        mock_svix: MagicMock,
    ) -> None:
        developer = DeveloperFactory()

        token = create_access_token(developer.id)

        mock_svix.is_enabled.return_value = False

        resp = client.get(
            "/api/v1/webhooks/endpoints",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Webhook fast lane — priority-set config (see calibra-ow-deploy
# docs/specs/2026-07-04-ow-webhook-fast-lane-design.md)
# ---------------------------------------------------------------------------


class TestWebhookPrioritySettings:
    def test_default_set_contains_the_nine_calibra_events(self) -> None:
        from app.config import Settings

        s = Settings()
        assert s.webhook_priority_event_set == frozenset(
            {
                "workout.created",
                "sleep.created",
                "series.heart_rate_variability_rmssd.created",
                "series.heart_rate_variability_sdnn.created",
                "series.respiratory_rate.created",
                "series.resting_heart_rate.created",
                "series.oxygen_saturation.created",
                "series.skin_temperature.created",
                "series.weight.created",
            }
        )

    def test_set_parses_comma_list_stripping_whitespace_and_empties(self) -> None:
        from app.config import Settings

        s = Settings(webhook_priority_events=" workout.created , sleep.created ,, ")
        assert s.webhook_priority_event_set == frozenset({"workout.created", "sleep.created"})

    def test_empty_value_yields_empty_set_fail_safe(self) -> None:
        from app.config import Settings

        s = Settings(webhook_priority_events="")
        assert s.webhook_priority_event_set == frozenset()


class TestPriorityQueueRouting:
    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_priority_event_routes_to_webhook_sync(self, mock_task: MagicMock) -> None:
        _dispatch("workout.created", {"k": "v"}, idempotency_key="idem-1")
        mock_task.apply_async.assert_called_once()
        call = mock_task.apply_async.call_args
        assert call.kwargs["queue"] == "webhook_sync"
        assert call.kwargs["args"] == ("workout.created", {"k": "v"})
        assert call.kwargs["kwargs"] == {"channels": None, "idempotency_key": "idem-1"}
        mock_task.delay.assert_not_called()

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_non_priority_event_keeps_default_delay(self, mock_task: MagicMock) -> None:
        _dispatch("sync.started", {"k": "v"})
        mock_task.delay.assert_called_once()
        mock_task.apply_async.assert_not_called()

    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_priority_set_override_is_respected(self, mock_task: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "webhook_priority_events", "sync.started")
        _dispatch("sync.started", {"k": "v"})
        assert mock_task.apply_async.call_args.kwargs["queue"] == "webhook_sync"
        _dispatch("workout.created", {"k": "v"})
        mock_task.delay.assert_called_once()  # no longer in the (overridden) set
