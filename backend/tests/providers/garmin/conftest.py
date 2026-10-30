"""A real HTTP server standing in for Garmin (Calibra 2.71.3.3).

The deregistration attempt is bounded by one total deadline, which a double of ``httpx`` cannot
exercise: a slow trickle of bytes has to arrive over a socket. So the fake sits below the client,
and every test reads what actually crossed the wire.
"""

import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from unittest.mock import PropertyMock, patch
from urllib.parse import parse_qs

import httpx
import pytest

from app.schemas.model_crud.credentials import ProviderEndpoints
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.strategy import GarminStrategy

TOKEN_PATH = "/di-oauth2-service/oauth/token"
REGISTRATION_PATH = "/partner-gateway/rest/user/registration"
REFRESHED_TOKEN_BODY = b'{"access_token": "refreshed-access", "token_type": "bearer", "expires_in": 86400}'

# Bound at import, before the suite's autouse mock_external_apis replaces httpx.AsyncClient. These
# tests are worth nothing against that mock: it answers every request with a MagicMock.
REAL_ASYNC_CLIENT = httpx.AsyncClient


@dataclass
class Reply:
    """What the fake answers on one route."""

    status: int = 200
    body: bytes = b""
    delay_seconds: float = 0.0
    trickle_seconds: float | None = None  # sends one body byte each interval
    hang_up: bool = False  # closes the socket without answering


@dataclass
class Call:
    method: str
    path: str
    authorization: str | None
    form: dict[str, list[str]]


@dataclass
class FakeGarmin:
    url: str = ""
    token: Reply = field(default_factory=lambda: Reply(body=REFRESHED_TOKEN_BODY))
    registration: Reply = field(default_factory=lambda: Reply(status=204))
    calls: list[Call] = field(default_factory=list)
    on_call: Callable[[Call], None] | None = None

    def paths(self) -> list[str]:
        return [call.path for call in self.calls]


def _handler(fake: FakeGarmin) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _serve(self, reply: Reply) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode()) if length else {}
            call = Call(self.command, self.path, self.headers.get("Authorization"), form)
            fake.calls.append(call)
            if fake.on_call is not None:
                fake.on_call(call)
            if reply.hang_up:
                self.close_connection = True
                self.connection.close()
                return
            time.sleep(reply.delay_seconds)
            self.send_response(reply.status)
            if reply.trickle_seconds is None:
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)
                return
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.flush()
            try:
                for index in range(len(reply.body)):
                    self.wfile.write(reply.body[index : index + 1])
                    self.wfile.flush()
                    time.sleep(reply.trickle_seconds)
            except OSError:
                self.close_connection = True

        def do_POST(self) -> None:
            self._serve(fake.token)

        def do_DELETE(self) -> None:
            self._serve(fake.registration)

    return Handler


@pytest.fixture
def fake_garmin() -> Generator[FakeGarmin, None, None]:
    """Serve Garmin's token and registration routes, and point the provider's OAuth at them."""
    fake = FakeGarmin()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_port}"
    endpoints = ProviderEndpoints(
        authorize_url=f"{fake.url}/oauth2Confirm",
        token_url=f"{fake.url}{TOKEN_PATH}",
    )
    with (
        patch("httpx.AsyncClient", REAL_ASYNC_CLIENT),
        patch.object(GarminOAuth, "endpoints", new_callable=PropertyMock, return_value=endpoints),
        patch.object(GarminStrategy, "api_base_url", new_callable=PropertyMock, return_value=fake.url),
    ):
        yield fake
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
