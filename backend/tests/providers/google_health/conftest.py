"""A real HTTP server standing in for Google's revoke endpoint (Calibra 2.72.3).

The revoke attempt is bounded by one total deadline, which a double of ``httpx`` cannot exercise:
a slow trickle of bytes has to arrive over a socket. So the fake sits below the client, as
Garmin's does (``tests/providers/garmin/conftest.py``), and every test reads what crossed the wire.
"""

from collections.abc import Generator
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import pytest

from app.services.providers.google_health import revocation
from tests.providers.garmin.conftest import REAL_ASYNC_CLIENT, Call, Reply, _handler

REVOKE_PATH = "/revoke"


@dataclass
class FakeGoogle:
    url: str = ""
    # Garmin's handler answers every POST with ``token``; Google's revoke is the one POST here.
    token: Reply = field(default_factory=lambda: Reply(status=200, body=b"{}"))
    registration: Reply = field(default_factory=Reply)
    calls: list[Call] = field(default_factory=list)
    on_call: None = None
    token_for: dict[str, Reply] = field(default_factory=dict)

    @property
    def revoke(self) -> Reply:
        return self.token

    @revoke.setter
    def revoke(self, reply: Reply) -> None:
        self.token = reply


@pytest.fixture
def fake_google() -> Generator[FakeGoogle, None, None]:
    """Serve Google's revoke route and point the attempt at it."""
    fake = FakeGoogle()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))  # type: ignore[arg-type]
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_port}"
    with (
        patch("httpx.AsyncClient", REAL_ASYNC_CLIENT),
        patch.object(revocation, "REVOKE_URL", f"{fake.url}{REVOKE_PATH}"),
    ):
        yield fake
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
