"""2.96.6.1 (spec cause 3, R-2026-10-06-b): the shared provider client logs a status, never a body.

An upstream error body can carry the user's data (a provider echoing the request, or the record
it refused). The client used to log it, or a message read from it, or the exception's text, at
error level, for every provider. Codex 4270734380.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.services.providers import api_client
from app.services.providers.api_client import make_authenticated_request

SENTINEL = "SENTINEL_HRV_73ms"


def _answer(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://example.test/v3/users/sleep"))


def _call() -> Any:
    return make_authenticated_request(
        db=MagicMock(),
        user_id=MagicMock(),
        connection_repo=MagicMock(),
        oauth=MagicMock(),
        api_base_url="https://example.test",
        provider_name="polar",
        endpoint="/v3/users/sleep",
    )


@pytest.fixture(autouse=True)
def _token() -> Any:
    with patch.object(api_client, "_get_valid_token", return_value="token"):
        yield


def _logged(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return caplog.text + captured.out + captured.err


@pytest.mark.parametrize("status", [400, 401, 404, 503])
def test_an_error_status_is_logged_as_its_status_without_the_body(
    status: int, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    answer = _answer(status, {"message": f"hrv {SENTINEL} for this user"})

    with patch.object(httpx.Client, "request", return_value=answer), pytest.raises(HTTPException) as raised:
        _call()

    assert raised.value.status_code == status
    logged = _logged(caplog, capsys)
    assert "API error" in logged
    assert f'"status_code": {status}' in logged
    assert SENTINEL not in logged


def test_an_error_in_a_200_body_is_logged_as_the_status_without_the_body(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # The "API returned error in body" path (Suunto-style): 200 with an error field.
    answer = _answer(200, {"error": f"hrv {SENTINEL}", "message": f"hrv {SENTINEL}", "code": 422})

    with patch.object(httpx.Client, "request", return_value=answer), pytest.raises(HTTPException):
        _call()

    logged = _logged(caplog, capsys)
    assert "API returned error in body" in logged
    assert '"status_code": 200' in logged
    assert SENTINEL not in logged


def test_a_request_that_fails_without_an_answer_is_logged_as_a_code_without_its_text(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    failure = httpx.ConnectError(f"refused, {SENTINEL}")

    with patch.object(httpx.Client, "request", side_effect=failure), pytest.raises(HTTPException):
        _call()

    logged = _logged(caplog, capsys)
    assert "API request failed" in logged
    assert "ConnectError" in logged
    assert SENTINEL not in logged
