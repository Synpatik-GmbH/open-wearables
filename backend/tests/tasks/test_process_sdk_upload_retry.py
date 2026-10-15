"""2.96.6.1 (spec 3.3, Apple Health cause 3): an SDK batch whose import failed for a transient
reason is retried from the payload it was queued with, instead of being dropped after its 202.

The SDK already holds its 202 for the batch, so it never re-sends it: the retry is the only way
a night in a failed batch can still be stored.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.process_sdk_upload_task import process_sdk_upload
from app.schemas.responses.upload.upload_response import UploadDataResponse
from app.services.sync_error_code import SDK_IMPORT_FAILED
from app.services.user_connection_service import user_connection_service
from tests.factories import UserFactory

_MODULE = "app.integrations.celery.tasks.process_sdk_upload_task"


@pytest.fixture
def task_env(db: Session, mock_celery_app: MagicMock) -> Iterator[dict[str, MagicMock]]:
    with (
        patch(f"{_MODULE}.SessionLocal") as session_local,
        patch(f"{_MODULE}.sdk_import_service") as importer,
        patch(f"{_MODULE}.emit_sync_failed") as failed,
        patch.object(process_sdk_upload, "retry", side_effect=Retry()) as retry,
    ):
        session_local.return_value.__enter__.return_value = db
        session_local.return_value.__exit__.return_value = None
        yield {"importer": importer, "failed": failed, "retry": retry}


def _run(user_id: Any) -> dict:
    return process_sdk_upload(
        content='{"data":{"sleep":[]}}', content_type="application/json", user_id=str(user_id), provider="apple"
    )


def test_a_transient_import_failure_is_retried_and_not_reported_failed(task_env: dict[str, MagicMock]) -> None:
    user = UserFactory()
    task_env["importer"].import_data_from_request.return_value = UploadDataResponse(
        status_code=400, response="Import failed: OperationalError", retryable=True
    )

    with pytest.raises(Retry):
        _run(user.id)

    task_env["retry"].assert_called_once()
    task_env["failed"].assert_not_called()


def test_an_invalid_batch_is_not_retried(task_env: dict[str, MagicMock]) -> None:
    user = UserFactory()
    task_env["importer"].import_data_from_request.return_value = UploadDataResponse(
        status_code=400, response="Validation failed: bad envelope"
    )

    _run(user.id)

    task_env["retry"].assert_not_called()
    task_env["failed"].assert_called_once()


def test_when_the_retries_are_spent_the_batch_is_reported_failed(task_env: dict[str, MagicMock]) -> None:
    user = UserFactory()
    task_env["importer"].import_data_from_request.return_value = UploadDataResponse(
        status_code=400, response="Import failed: OperationalError", retryable=True
    )
    process_sdk_upload.push_request(retries=process_sdk_upload.max_retries)
    try:
        _run(user.id)
    finally:
        process_sdk_upload.pop_request()

    task_env["retry"].assert_not_called()
    task_env["failed"].assert_called_once()


def _at_attempt(retries: int, call: Any) -> Any:
    process_sdk_upload.push_request(retries=retries)
    try:
        return call()
    finally:
        process_sdk_upload.pop_request()


def _retryable_import_failure(task_env: dict[str, MagicMock]) -> None:
    task_env["importer"].import_data_from_request.return_value = UploadDataResponse(
        status_code=400, response="Import failed: OperationalError", retryable=True
    )


class TestRetrySchedule:
    """Five retries, 30 seconds doubling. Written out, so a different count or delay is red."""

    def test_each_retry_waits_twice_as_long_as_the_last(self, task_env: dict[str, MagicMock]) -> None:
        user = UserFactory()
        _retryable_import_failure(task_env)

        for retries in range(5):
            with pytest.raises(Retry):
                _at_attempt(retries, lambda: _run(user.id))

        assert [c.kwargs["countdown"] for c in task_env["retry"].call_args_list] == [30, 60, 120, 240, 480]

    def test_the_fifth_retry_is_the_last(self, task_env: dict[str, MagicMock]) -> None:
        user = UserFactory()
        _retryable_import_failure(task_env)

        with pytest.raises(Retry):
            _at_attempt(4, lambda: _run(user.id))
        _at_attempt(5, lambda: _run(user.id))

        task_env["retry"].assert_called_once()
        task_env["failed"].assert_called_once()


# FORK (2.96.6.1, Dragan's review of #40, item 1): the three steps before the import. Each one,
# failing, would otherwise drop a batch the SDK already holds a 202 for.
_DOWN = OperationalError("SELECT 1", {}, Exception("connection refused"))


@contextmanager
def _user_check_fails() -> Iterator[None]:
    with patch(f"{_MODULE}.UserRepository") as repository:
        repository.return_value.get.side_effect = _DOWN
        yield


@contextmanager
def _connection_setup_fails() -> Iterator[None]:
    with patch.object(user_connection_service, "ensure_sdk_connection", side_effect=_DOWN):
        yield


@contextmanager
def _s3_load_fails() -> Iterator[None]:
    with patch(f"{_MODULE}.get_payload_from_s3", side_effect=_DOWN):
        yield


_PRE_IMPORT_FAILURES = {
    "user_check": _user_check_fails,
    "connection_setup": _connection_setup_fails,
    "s3_load": _s3_load_fails,
}


def _run_from_s3(user_id: Any) -> dict:
    return process_sdk_upload(
        content=None,
        content_type="application/json",
        user_id=str(user_id),
        provider="apple",
        payload_ref="s3://payloads/batch.json",
    )


@pytest.mark.parametrize("step", sorted(_PRE_IMPORT_FAILURES))
def test_a_failure_before_the_import_is_retried_and_not_reported_failed(
    task_env: dict[str, MagicMock], step: str
) -> None:
    user = UserFactory()

    with _PRE_IMPORT_FAILURES[step](), pytest.raises(Retry):
        _at_attempt(2, lambda: _run_from_s3(user.id) if step == "s3_load" else _run(user.id))

    assert [c.kwargs["countdown"] for c in task_env["retry"].call_args_list] == [120]
    task_env["failed"].assert_not_called()
    task_env["importer"].import_data_from_request.assert_not_called()


@pytest.mark.parametrize("step", sorted(_PRE_IMPORT_FAILURES))
def test_a_failure_before_the_import_with_no_retry_left_is_reported_failed_and_raised(
    task_env: dict[str, MagicMock], step: str
) -> None:
    user = UserFactory()

    with _PRE_IMPORT_FAILURES[step](), pytest.raises(OperationalError):
        _at_attempt(5, lambda: _run_from_s3(user.id) if step == "s3_load" else _run(user.id))

    task_env["retry"].assert_not_called()
    task_env["failed"].assert_called_once()
    failed = task_env["failed"].call_args
    assert failed.args[0] == user.id
    assert failed.kwargs["error"] == SDK_IMPORT_FAILED
    assert failed.kwargs["metadata"]["cause"] == "OperationalError"


@pytest.mark.parametrize("step", sorted(_PRE_IMPORT_FAILURES))
def test_a_failure_before_the_import_is_still_retried_on_the_fifth_attempt(
    task_env: dict[str, MagicMock], step: str
) -> None:
    # Dragan's re-review of #40: with the case above (no retry left at the sixth attempt), this
    # pins five retries before the import too, not only in it.
    user = UserFactory()

    with _PRE_IMPORT_FAILURES[step](), pytest.raises(Retry):
        _at_attempt(4, lambda: _run_from_s3(user.id) if step == "s3_load" else _run(user.id))

    assert [c.kwargs["countdown"] for c in task_env["retry"].call_args_list] == [480]
    task_env["failed"].assert_not_called()
