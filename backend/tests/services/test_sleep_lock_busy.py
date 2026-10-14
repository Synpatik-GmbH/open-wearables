"""2.96.6.1 (spec 3.3, Apple Health cause 3): a batch whose sleep could not take the per-user
lock is not silently dropped. It raises, so the import reports a retryable failure and the
upload task processes the batch again."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.schemas.providers.mobile_sdk import SyncRequest
from app.services.sdk.import_service import ImportService, import_service
from app.services.sdk.sleep_service import SleepLockBusyError, handle_sleep_data
from tests.factories import UserFactory
from tests.services.test_sleep_service import OLD_WATCH_PAYLOAD


@patch("app.services.sdk.sleep_service.get_redis_client")
def test_a_sleep_batch_that_cannot_take_the_lock_raises(mock_redis: MagicMock, db: Session) -> None:
    mock_redis.return_value.lock.return_value.acquire.return_value = False

    with pytest.raises(SleepLockBusyError):
        handle_sleep_data(db, SyncRequest.model_validate(OLD_WATCH_PAYLOAD), str(uuid4()), raise_when_busy=True)


@patch("app.services.sdk.sleep_service.get_redis_client")
def test_other_callers_still_skip_a_batch_whose_lock_is_busy(mock_redis: MagicMock, db: Session) -> None:
    # The XML and AWS imports call it per chunk with no retry: raising there would abort the file.
    mock_redis.return_value.lock.return_value.acquire.return_value = False

    handle_sleep_data(db, SyncRequest.model_validate(OLD_WATCH_PAYLOAD), str(uuid4()))


@patch("app.services.sdk.sleep_service.get_redis_client")
def test_the_sdk_import_raises_on_a_busy_lock(mock_redis: MagicMock, db: Session) -> None:
    mock_redis.return_value.lock.return_value.acquire.return_value = False
    user = UserFactory()

    with pytest.raises(SleepLockBusyError):
        import_service.load_data(db, OLD_WATCH_PAYLOAD, user_id=str(user.id))


def test_an_unexpected_import_failure_is_retryable_and_reports_a_code(db: Session) -> None:
    service = ImportService.__new__(ImportService)
    service.log = MagicMock()
    with (
        patch.object(ImportService, "_parse_json_content", return_value={"provider": "apple", "data": {}}),
        patch.object(ImportService, "load_data", side_effect=SleepLockBusyError()),
    ):
        response = service.import_data_from_request(db, "{}", "application/json", str(uuid4()))

    assert response.retryable is True
    assert response.response == "Import failed: SleepLockBusyError"
