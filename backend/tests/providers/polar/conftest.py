"""A real HTTP server standing in for Polar AccessLink's deregistration route (Calibra GL-20).

The attempt is bounded by one total deadline, which a double of ``httpx`` cannot exercise, so the
fake sits below the client as Garmin's and Google's do, and every test reads what crossed the wire.
"""

from collections.abc import Generator
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import PropertyMock, patch

import pytest

from app.services.providers.polar.strategy import PolarStrategy
from tests.providers.garmin.conftest import REAL_ASYNC_CLIENT, FakeGarmin, _handler

POLAR_USER_ID = "61234567"
DEREGISTRATION_PATH = f"/v3/users/{POLAR_USER_ID}"

# Garmin's fake answers every DELETE with ``registration``: Polar's deregistration is the one DELETE.
FakePolar = FakeGarmin


@pytest.fixture
def fake_polar() -> Generator[FakePolar, None, None]:
    """Serve Polar's deregistration route and point the provider at it."""
    fake = FakePolar()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_port}"
    with (
        patch("httpx.AsyncClient", REAL_ASYNC_CLIENT),
        patch.object(PolarStrategy, "api_base_url", new_callable=PropertyMock, return_value=fake.url),
    ):
        yield fake
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
