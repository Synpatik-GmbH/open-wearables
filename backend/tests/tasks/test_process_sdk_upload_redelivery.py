"""2.96.6.3 (spec C1, AC2-AC4): an SDK batch is acknowledged when it ends, so a batch whose
worker is killed is delivered again; a redo writes no duplicates; and a batch whose three
deliveries before were each killed is refused instead of killing the worker a fourth time.

A real kill is in ``test_process_sdk_upload_worker_kill.py``. Here a kill is ``_WorkerKilled``: a
``BaseException``, so nothing after it in the task runs, ``except Exception`` included, as nothing
runs after SIGKILL.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from celery.exceptions import Retry
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.process_sdk_upload_task import (
    SDK_UPLOAD_DELIVERY_TTL_SECONDS,
    SDK_UPLOAD_MAX_DELIVERIES,
    process_sdk_upload,
)
from app.integrations.redis_client import get_redis_client
from app.models import DataPointSeries, DataSource, EventRecord, SleepDetails
from app.schemas.responses.upload.upload_response import UploadDataResponse
from app.schemas.sync_status import SyncScope
from app.services.sdk.sleep_service import finish_sleep, load_sleep_state
from app.services.sync_error_code import SDK_IMPORT_FAILED, SDK_UPLOAD_DELIVERY_CAP
from tests.factories import UserFactory
from tests.tasks.sdk_upload_payload import night_payload

_MODULE = "app.integrations.celery.tasks.process_sdk_upload_task"
_OK = UploadDataResponse(status_code=200, response="Import successful")


class _WorkerKilled(BaseException):
    """Stands in for the kernel's SIGKILL: nothing after it in the task runs."""


def _key(batch_id: str) -> str:
    return f"sdk_upload:deliveries:{batch_id}"


def _run(user_id: Any, batch_id: str, content: str = '{"data":{"sleep":[]}}', **batch: Any) -> dict:
    return process_sdk_upload(
        content=content,
        content_type="application/json",
        user_id=str(user_id),
        provider="apple",
        batch_id=batch_id,
        **batch,
    )


# Both kinds of batch the SDK sends: live sync, and one batch of a historical export.
_SCOPES = {
    SyncScope.LIVE: {},
    SyncScope.HISTORICAL: {"sync_type": "historical", "sync_session_id": "export-1"},
}


@pytest.fixture
def task_env(db: Session, mock_celery_app: MagicMock) -> Iterator[dict[str, MagicMock]]:
    with (
        patch(f"{_MODULE}.SessionLocal") as session_local,
        patch(f"{_MODULE}.sdk_import_service") as importer,
        patch(f"{_MODULE}.emit_sync_failed") as failed,
        patch(f"{_MODULE}.emit_sync_completed") as completed,
        patch.object(process_sdk_upload, "retry", side_effect=Retry()) as retry,
    ):
        session_local.return_value.__enter__.return_value = db
        session_local.return_value.__exit__.return_value = None
        yield {"importer": importer, "failed": failed, "completed": completed, "retry": retry}


def test_the_task_is_acknowledged_when_it_ends() -> None:
    """Spec AC1. Without acks_late a thread acknowledges the batch when it starts it, so a kill
    loses it; reject_on_worker_lost keeps the redelivery on a prefork worker."""
    assert process_sdk_upload.acks_late is True
    assert process_sdk_upload.reject_on_worker_lost is True


class TestDeliveryCap:
    """Spec C1, AC3: deliveries 1 to 3 are processed; the one after three killed deliveries is not."""

    @pytest.mark.parametrize("scope", list(_SCOPES))
    def test_the_fourth_delivery_after_three_kills_is_recorded_failed_and_not_processed(
        self, task_env: dict[str, MagicMock], scope: SyncScope
    ) -> None:
        user = UserFactory()
        batch_id = str(uuid4())
        task_env["importer"].import_data_from_request.side_effect = _WorkerKilled()

        for delivery in range(1, SDK_UPLOAD_MAX_DELIVERIES + 1):
            with pytest.raises(_WorkerKilled):
                _run(user.id, batch_id, **_SCOPES[scope])
            assert task_env["importer"].import_data_from_request.call_count == delivery
            assert get_redis_client().get(_key(batch_id)) == str(delivery)

        result = _run(user.id, batch_id, **_SCOPES[scope])

        assert task_env["importer"].import_data_from_request.call_count == SDK_UPLOAD_MAX_DELIVERIES
        assert result == {"status": "error", "reason": SDK_UPLOAD_DELIVERY_CAP, "batch_id": batch_id}
        task_env["failed"].assert_called_once()
        recorded = task_env["failed"].call_args.kwargs
        assert recorded["scope"] == scope
        assert recorded["error"] == SDK_IMPORT_FAILED
        assert recorded["metadata"] == {"batch_id": batch_id, "cause": SDK_UPLOAD_DELIVERY_CAP}
        task_env["retry"].assert_not_called()
        # Refused and acknowledged, so the count goes with it.
        assert get_redis_client().exists(_key(batch_id)) == 0

    def test_the_count_holds_a_number_and_expires_within_a_day(self, task_env: dict[str, MagicMock]) -> None:
        """Data-protection condition: identifiers and a number only, and it expires."""
        user = UserFactory()
        batch_id = str(uuid4())
        task_env["importer"].import_data_from_request.side_effect = _WorkerKilled()

        with pytest.raises(_WorkerKilled):
            _run(user.id, batch_id)

        assert get_redis_client().keys("sdk_upload:*") == [_key(batch_id)]
        assert get_redis_client().get(_key(batch_id)) == "1"
        assert 0 < get_redis_client().ttl(_key(batch_id)) <= SDK_UPLOAD_DELIVERY_TTL_SECONDS == 24 * 3600

    def test_a_batch_queued_without_a_batch_id_is_counted_by_its_task_id(self, task_env: dict[str, MagicMock]) -> None:
        """A generated id would be new on every delivery, so the cap would never see a fourth."""
        user = UserFactory()
        task_id = str(uuid4())
        task_env["importer"].import_data_from_request.side_effect = _WorkerKilled()

        def deliver() -> dict:
            process_sdk_upload.push_request(id=task_id)
            try:
                return process_sdk_upload(
                    content='{"data":{"sleep":[]}}',
                    content_type="application/json",
                    user_id=str(user.id),
                    provider="apple",
                )
            finally:
                process_sdk_upload.pop_request()

        for _ in range(SDK_UPLOAD_MAX_DELIVERIES):
            with pytest.raises(_WorkerKilled):
                deliver()

        assert deliver()["reason"] == SDK_UPLOAD_DELIVERY_CAP
        assert task_env["importer"].import_data_from_request.call_count == SDK_UPLOAD_MAX_DELIVERIES

    def test_a_batch_whose_count_cannot_be_kept_is_processed(self, task_env: dict[str, MagicMock]) -> None:
        """The cap protects the worker; losing the batch is what the task exists to prevent."""
        user = UserFactory()
        task_env["importer"].import_data_from_request.return_value = _OK
        broken = MagicMock()
        broken.pipeline.side_effect = ConnectionError("redis down")
        broken.delete.side_effect = ConnectionError("redis down")

        with patch(f"{_MODULE}.get_redis_client", return_value=broken):
            result = _run(user.id, str(uuid4()))

        assert result["status_code"] == 200
        task_env["importer"].import_data_from_request.assert_called_once()
        task_env["failed"].assert_not_called()

    def test_each_delivery_refreshes_the_expiry(self, task_env: dict[str, MagicMock]) -> None:
        user = UserFactory()
        batch_id = str(uuid4())
        task_env["importer"].import_data_from_request.side_effect = _WorkerKilled()
        with pytest.raises(_WorkerKilled):
            _run(user.id, batch_id)
        get_redis_client().expire(_key(batch_id), 60)

        with pytest.raises(_WorkerKilled):
            _run(user.id, batch_id)

        assert get_redis_client().ttl(_key(batch_id)) > 60


class TestEveryOtherExitResetsTheCount:
    """C1 counts consecutive killed deliveries (decision, Andrei, 20 Oct 2026)."""

    @pytest.mark.parametrize(
        "outcome",
        ["saved", "recorded_failed", "user_not_found"],
    )
    def test_a_delivery_that_ends_leaves_no_count(self, task_env: dict[str, MagicMock], outcome: str) -> None:
        user = UserFactory()
        batch_id = str(uuid4())
        task_env["importer"].import_data_from_request.side_effect = [_WorkerKilled(), _WorkerKilled()]
        for _ in range(2):
            with pytest.raises(_WorkerKilled):
                _run(user.id, batch_id)
        assert get_redis_client().get(_key(batch_id)) == "2"
        task_env["importer"].import_data_from_request.side_effect = None
        task_env["importer"].import_data_from_request.return_value = (
            _OK if outcome == "saved" else UploadDataResponse(status_code=400, response="Validation failed")
        )

        # An early return: the user the batch names has gone.
        _run(user.id if outcome != "user_not_found" else uuid4(), batch_id)

        assert get_redis_client().exists(_key(batch_id)) == 0

    def test_a_scheduled_retry_resets_the_count_so_the_retry_is_processed(self, task_env: dict[str, MagicMock]) -> None:
        """Two kills, then a retryable failure: the 2.96.6.1 retry is not a killed delivery, so the
        retry that follows is processed rather than refused as a fourth."""
        user = UserFactory()
        batch_id = str(uuid4())
        importer = task_env["importer"].import_data_from_request
        importer.side_effect = [
            _WorkerKilled(),
            _WorkerKilled(),
            UploadDataResponse(status_code=400, response="Import failed: OperationalError", retryable=True),
            _OK,
        ]
        for _ in range(2):
            with pytest.raises(_WorkerKilled):
                _run(user.id, batch_id)
        with pytest.raises(Retry):
            _run(user.id, batch_id)
        assert get_redis_client().exists(_key(batch_id)) == 0

        process_sdk_upload.push_request(retries=1)
        try:
            result = _run(user.id, batch_id)
        finally:
            process_sdk_upload.pop_request()

        assert importer.call_count == 4
        assert result["status_code"] == 200
        task_env["failed"].assert_not_called()


def test_a_batch_saved_on_its_first_delivery_behaves_as_today(task_env: dict[str, MagicMock]) -> None:
    """Spec AC4."""
    user = UserFactory()
    batch_id = str(uuid4())
    task_env["importer"].import_data_from_request.return_value = _OK

    result = _run(user.id, batch_id)

    assert result == {**_OK.model_dump(), "batch_id": batch_id}
    task_env["importer"].import_data_from_request.assert_called_once()
    task_env["completed"].assert_called_once()
    task_env["failed"].assert_not_called()
    task_env["retry"].assert_not_called()
    assert get_redis_client().exists(_key(batch_id)) == 0


def _stored(db: Session, user_id: UUID) -> dict[str, Any]:
    sources = select(DataSource.id).where(DataSource.user_id == user_id)

    def count(model: Any, *where: Any) -> int:
        return db.scalar(select(func.count()).select_from(model).where(model.data_source_id.in_(sources), *where)) or 0

    return {
        "series": count(DataPointSeries),
        "workouts": count(EventRecord, EventRecord.category == "workout"),
        "sleep": _nights(db, user_id),
    }


def _nights(db: Session, user_id: UUID) -> list[tuple]:
    """Each stored night with its minutes, so a night whose stages were counted twice differs."""
    sources = select(DataSource.id).where(DataSource.user_id == user_id)
    rows = db.execute(
        select(EventRecord, SleepDetails)
        .join(SleepDetails, SleepDetails.record_id == EventRecord.id)
        .where(EventRecord.data_source_id.in_(sources), EventRecord.category == "sleep")
    )
    return [
        (
            record.start_datetime,
            record.end_datetime,
            record.duration_seconds,
            detail.sleep_total_duration_minutes,
            detail.sleep_light_minutes,
            detail.sleep_deep_minutes,
            detail.sleep_rem_minutes,
            detail.sleep_awake_minutes,
            detail.sleep_time_in_bed_minutes,
        )
        for record, detail in rows
    ]


def test_a_redone_batch_writes_no_duplicate_series_workouts_or_sleep(db: Session, mock_celery_app: MagicMock) -> None:
    """Spec AC2. The real import, twice over one batch: what a redo after a kill does to rows the
    first delivery already committed."""
    user = UserFactory()
    batch_id = str(uuid4())
    with (
        patch(f"{_MODULE}.SessionLocal") as session_local,
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps"),
    ):
        session_local.return_value.__enter__.return_value = db
        session_local.return_value.__exit__.return_value = None

        first = _run(user.id, batch_id, night_payload())
        after_first = _stored(db, user.id)
        redo = _run(user.id, batch_id, night_payload())
        after_redo = _stored(db, user.id)

    assert first["status_code"] == 200
    # A redo that fails writes nothing, which would also leave the counts equal.
    assert redo["status_code"] == 200
    assert after_first["series"] == 5
    assert after_first["workouts"] == 1
    assert len(after_first["sleep"]) == 1
    assert after_redo == after_first


def test_a_redone_batch_of_a_night_still_open_counts_its_stages_once(db: Session, mock_celery_app: MagicMock) -> None:
    """Spec AC2, the other sleep branch: a night that ended less than the gap ago stays open in
    Redis, and a redo appends the same stages to it again. The night it is finished into must be
    the night one delivery makes."""
    once, redone = UserFactory(), UserFactory()
    night = night_payload(datetime.now(timezone.utc) - timedelta(hours=8, minutes=30))
    with (
        patch(f"{_MODULE}.SessionLocal") as session_local,
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps"),
    ):
        session_local.return_value.__enter__.return_value = db
        session_local.return_value.__exit__.return_value = None

        assert _run(once.id, str(uuid4()), night)["status_code"] == 200
        batch_id = str(uuid4())
        assert _run(redone.id, batch_id, night)["status_code"] == 200
        assert _run(redone.id, batch_id, night)["status_code"] == 200

    for user in (once, redone):
        state = load_sleep_state(str(user.id))
        assert state is not None, "the night was finished in the batch; this test needs it open"
        finish_sleep(db, str(user.id), state)

    assert len(_nights(db, once.id)) == 1
    assert _nights(db, redone.id) == _nights(db, once.id)
