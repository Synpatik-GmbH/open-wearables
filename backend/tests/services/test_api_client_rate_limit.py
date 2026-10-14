"""2.96.6.1 (spec R1, cause 3): a caller can opt out of retrying a 429.

Polar's limits are a 15-minute and a 24-hour window. Retrying after 1, 2 and 4 seconds cannot
clear either, and each retry is one more call counted against the limit.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.services.providers import api_client
from app.services.providers.api_client import make_authenticated_request


class _Client:
    """Stands in for httpx.Client; answers 429 and counts requests."""

    requests = 0

    def __init__(self, **_: Any) -> None:
        pass

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, **_: Any) -> httpx.Response:
        type(self).requests += 1
        return httpx.Response(429, text="slow down", request=httpx.Request("GET", "https://example.test"))


@pytest.fixture
def client() -> type[_Client]:
    _Client.requests = 0
    with (
        patch.object(api_client.httpx, "Client", _Client),
        patch.object(api_client, "_get_valid_token", return_value="token"),
        patch.object(api_client.time, "sleep"),
    ):
        yield _Client


def _call(**kwargs: Any) -> Any:
    return make_authenticated_request(
        db=MagicMock(),
        user_id=MagicMock(),
        connection_repo=MagicMock(),
        oauth=MagicMock(),
        api_base_url="https://example.test",
        provider_name="polar",
        endpoint="/v3/users/activities",
        **kwargs,
    )


def test_a_429_is_raised_after_one_request_when_the_caller_opts_out_of_retrying(client: type[_Client]) -> None:
    with pytest.raises(HTTPException) as raised:
        _call(retry_on_rate_limit=False)

    assert raised.value.status_code == 429
    assert client.requests == 1


def test_a_429_is_still_retried_by_default(client: type[_Client]) -> None:
    # Every other provider keeps the behaviour it had.
    with pytest.raises(HTTPException):
        _call()

    assert client.requests == api_client.MAX_RETRIES + 1
