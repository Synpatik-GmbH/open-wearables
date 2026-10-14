"""2.96.6.1 (spec 3.3, Apple Health cause 3): an SDK batch whose import failed for a transient
reason is retried from the payload it was queued with, instead of being dropped after its 202.

The SDK already holds its 202 for the batch, so it never re-sends it: the retry is the only way
a night in a failed batch can still be stored.
"""

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.process_sdk_upload_task import process_sdk_upload
from app.schemas.responses.upload.upload_response import UploadDataResponse
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
