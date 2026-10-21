from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WearableHistorySource
from tests.factories import ApiKeyFactory, ApplicationFactory, UserFactory
from tests.utils import api_key_headers


def test_apple_token_begins_history_before_first_upload_and_retries_preserve_it(
    client: TestClient, db: Session
) -> None:
    application = ApplicationFactory(app_secret="history-test-secret")
    member = UserFactory()
    body = {"app_id": application.app_id, "app_secret": "history-test-secret"}
    for _ in range(2):
        assert client.post(f"/api/v1/users/{member.id}/token", json=body).status_code == 200
    sources = list(db.scalars(select(WearableHistorySource).where(WearableHistorySource.user_id == member.id)))
    assert len(sources) == 1
    assert sources[0].provider == "apple"
    assert sources[0].start_date == sources[0].connection_day


def test_source_metadata_is_api_key_only_and_is_scoped_to_the_member(client: TestClient, db: Session) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository

    member, other = UserFactory(), UserFactory()
    WearableHistoryRepository().begin_source(db, member.id, "polar")
    WearableHistoryRepository().begin_source(db, other.id, "apple")
    key = ApiKeyFactory()
    url = f"/api/v1/users/{member.id}/wearable-history-sources"
    assert client.get(url).status_code == 401
    response = client.get(url, headers=api_key_headers(key.plain_key))
    assert response.status_code == 200
    assert [source["provider"] for source in response.json()] == ["polar"]
    assert set(response.json()[0]) == {"provider", "connection_day", "start_date", "freeze_at"}
    application = ApplicationFactory(app_secret="history-api-scope-secret")
    token = client.post(
        f"/api/v1/users/{member.id}/token",
        json={"app_id": application.app_id, "app_secret": "history-api-scope-secret"},
    ).json()["access_token"]
    assert client.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize("provider", ["apple", "health_connect", "samsung", "google"])
def test_first_sdk_upload_establishes_its_own_source_before_dispatch(
    client: TestClient, db: Session, provider: str
) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository

    member = UserFactory()
    WearableHistoryRepository().begin_source(db, member.id, "apple")
    key = ApiKeyFactory()
    with (
        patch("app.api.routes.v1.sdk_sync.process_sdk_upload.delay") as dispatch,
        patch("app.api.routes.v1.sdk_sync.load_sleep_state", return_value=None),
        patch("app.api.routes.v1.sdk_sync.store_raw_payload"),
        patch("app.api.routes.v1.sdk_sync.settings.sdk_payload_s3_offload", False),
    ):
        response = client.post(
            f"/api/v1/sdk/users/{member.id}/sync",
            headers=api_key_headers(key.plain_key),
            json={"provider": provider, "data": {"records": [], "sleep": [], "workouts": []}},
        )
    assert response.status_code == 202
    dispatch.assert_called_once()
    canonical = "health_connect" if provider == "google" else provider
    source = WearableHistoryRepository().get(db, member.id, canonical)
    assert source is not None
    assert source.start_date == source.connection_day
