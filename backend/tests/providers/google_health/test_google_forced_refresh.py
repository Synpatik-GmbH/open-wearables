"""A rejected Google data call forces one refresh; a rejected refresh ends the connection
(Calibra 2.72.3, design D-09; spec 2.7 and AC 18).

Google sends no revoke notification this fork can read, so a grant the member removed in their
Google account is found at the next data call. A real server stands in for Google's data and token
endpoints, because the path under test is the real request loop and the real refresh.
"""

from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from unittest.mock import PropertyMock, patch
from urllib.parse import parse_qs

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import UserConnection
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.model_crud.credentials import ProviderEndpoints
from app.services.providers.api_client import make_authenticated_request
from app.services.providers.google_health.oauth import GoogleOAuth
from app.services.providers.google_health.strategy import GoogleHealthStrategy
from app.services.providers.polar.oauth import PolarOAuth
from app.services.providers.polar.strategy import PolarStrategy
from tests.factories import UserConnectionFactory, UserFactory

TOKEN_OK = b'{"access_token": "fresh-access", "token_type": "Bearer", "expires_in": 3599}'
TOKEN_REJECTED = b'{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}'


@dataclass
class FakeGoogleApi:
    url: str = ""
    data_statuses: list[int] = field(default_factory=lambda: [200])
    token_status: int = 200
    token_body: bytes = TOKEN_OK
    data_auth: list[str | None] = field(default_factory=list)
    token_forms: list[dict[str, list[str]]] = field(default_factory=list)


def _handler(fake: FakeGoogleApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _answer(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            fake.data_auth.append(self.headers.get("Authorization"))
            status = fake.data_statuses.pop(0) if len(fake.data_statuses) > 1 else fake.data_statuses[0]
            self._answer(status, b'{"dataPoints": []}' if status == 200 else b'{"error": {"code": 401}}')

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            fake.token_forms.append(parse_qs(self.rfile.read(length).decode()))
            self._answer(fake.token_status, fake.token_body)

    return Handler


@pytest.fixture
def fake_api() -> Generator[FakeGoogleApi, None, None]:
    fake = FakeGoogleApi()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_port}"
    endpoints = ProviderEndpoints(authorize_url=f"{fake.url}/auth", token_url=f"{fake.url}/token")
    with (
        patch.object(GoogleOAuth, "endpoints", new_callable=PropertyMock, return_value=endpoints),
        patch.object(PolarOAuth, "endpoints", new_callable=PropertyMock, return_value=endpoints),
    ):
        yield fake
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def connection(db: Session, provider: str = "google_health", **overrides: Any) -> UserConnection:
    values: dict[str, Any] = {
        "provider": provider,
        "status": ConnectionStatus.ACTIVE,
        "access_token": "stale-access",
        "refresh_token": "held-refresh",
        # Far from expiry, so today's path would never refresh it.
        "token_expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    values.update(overrides)
    return UserConnectionFactory(user=UserFactory(), **values)


def call(db: Session, row: UserConnection, fake: FakeGoogleApi, provider: str = "google_health") -> Any:
    strategy = GoogleHealthStrategy() if provider == "google_health" else PolarStrategy()
    return make_authenticated_request(
        db, row.user_id, UserConnectionRepository(), strategy.oauth, fake.url, provider, "/data"
    )


class TestARejectedCallForcesOneRefresh:
    def test_a_401_refreshes_whatever_the_expiry_and_retries_once_with_the_new_token(
        self, db: Session, fake_api: FakeGoogleApi
    ) -> None:
        row = connection(db)
        fake_api.data_statuses = [401, 200]

        assert call(db, row, fake_api) == {"dataPoints": []}

        assert fake_api.data_auth == ["Bearer stale-access", "Bearer fresh-access"]
        assert [form["refresh_token"] for form in fake_api.token_forms] == [["held-refresh"]]
        db.refresh(row)
        assert (row.status, row.access_token) == (ConnectionStatus.ACTIVE, "fresh-access")

    def test_a_second_401_raises_with_no_second_refresh(self, db: Session, fake_api: FakeGoogleApi) -> None:
        row = connection(db)
        fake_api.data_statuses = [401, 401, 200]

        with pytest.raises(HTTPException) as raised:
            call(db, row, fake_api)

        assert raised.value.status_code == 401
        assert len(fake_api.token_forms) == 1
        assert len(fake_api.data_auth) == 2

    def test_a_200_makes_no_refresh(self, db: Session, fake_api: FakeGoogleApi) -> None:
        row = connection(db)

        call(db, row, fake_api)

        assert fake_api.token_forms == []

    def test_a_row_with_no_refresh_token_raises_with_no_refresh(self, db: Session, fake_api: FakeGoogleApi) -> None:
        row = connection(db, refresh_token=None)
        fake_api.data_statuses = [401]

        with pytest.raises(HTTPException) as raised:
            call(db, row, fake_api)

        assert raised.value.status_code == 401
        assert fake_api.token_forms == []


class TestARejectedRefreshEndsTheConnection:
    def test_the_row_is_revoked_with_no_tokens_and_connection_revoked_goes_out(
        self, db: Session, fake_api: FakeGoogleApi
    ) -> None:
        row = connection(db)
        fake_api.data_statuses = [401]
        fake_api.token_status = 400
        fake_api.token_body = TOKEN_REJECTED

        with (
            patch("app.services.providers.templates.base_oauth.on_connection_revoked") as revoked,
            pytest.raises(HTTPException) as raised,
        ):
            call(db, row, fake_api)

        assert raised.value.status_code == 401
        db.refresh(row)
        assert (row.status, row.access_token, row.refresh_token, row.token_expires_at) == (
            ConnectionStatus.REVOKED,
            None,
            None,
            None,
        )
        revoked.assert_called_once()
        assert revoked.call_args.kwargs["provider"] == "google_health"
        assert revoked.call_args.kwargs["reason"] == "refresh_failed"
        assert len(fake_api.data_auth) == 1


class TestAnotherProvider:
    def test_a_polar_401_raises_as_today_with_no_refresh(self, db: Session, fake_api: FakeGoogleApi) -> None:
        row = connection(db, provider="polar")
        fake_api.data_statuses = [401, 200]

        with pytest.raises(HTTPException) as raised:
            call(db, row, fake_api, provider="polar")

        assert raised.value.status_code == 401
        assert fake_api.token_forms == []
        assert len(fake_api.data_auth) == 1


class TestARevokedGoogleRowHoldsNoToken:
    def test_mark_as_revoked_clears_a_google_rows_tokens(self, db: Session) -> None:
        row = connection(db)

        UserConnectionRepository().mark_as_revoked(db, row)

        db.refresh(row)
        assert (row.status, row.access_token, row.refresh_token, row.token_expires_at) == (
            ConnectionStatus.REVOKED,
            None,
            None,
            None,
        )

    def test_mark_as_revoked_keeps_another_providers_tokens_as_today(self, db: Session) -> None:
        row = connection(db, provider="polar")

        UserConnectionRepository().mark_as_revoked(db, row)

        db.refresh(row)
        assert (row.status, row.refresh_token) == (ConnectionStatus.REVOKED, "held-refresh")
