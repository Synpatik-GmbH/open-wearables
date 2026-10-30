"""One deregistration attempt at Garmin (Calibra 2.71.3.3, design D-05; spec P15 and AC 14)."""

import json
import time
from uuid import uuid4

import pytest

from app.config import settings
from app.services.providers.garmin import deregistration
from app.services.providers.garmin.deregistration import (
    DeregistrationOutcome,
    DeregistrationTrigger,
    HeldTokens,
    attempt_deregistration,
)
from app.services.providers.garmin.strategy import GarminStrategy
from tests.providers.garmin.conftest import REFRESHED_TOKEN_BODY, REGISTRATION_PATH, TOKEN_PATH, FakeGarmin, Reply

HELD = HeldTokens(access_token="held-access", refresh_token="held-refresh")


def attempt(held: HeldTokens = HELD) -> DeregistrationOutcome:
    return attempt_deregistration(
        GarminStrategy().oauth,
        held,
        trigger=DeregistrationTrigger.DISCONNECT,
        user_id=uuid4(),
    )


def logged_lines(out: str) -> list[dict]:
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "garmin_deregistration"]


class TestOutcome:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (200, DeregistrationOutcome.CONFIRMED),
            (204, DeregistrationOutcome.CONFIRMED),
            (404, DeregistrationOutcome.NOT_FOUND),
            (401, DeregistrationOutcome.FAILED),
            (403, DeregistrationOutcome.FAILED),
            (429, DeregistrationOutcome.FAILED),
            (500, DeregistrationOutcome.FAILED),
            (503, DeregistrationOutcome.FAILED),
        ],
    )
    def test_the_answer_to_the_deregistration_decides(
        self, fake_garmin: FakeGarmin, status: int, expected: DeregistrationOutcome
    ) -> None:
        fake_garmin.registration = Reply(status=status)

        assert attempt() is expected
        assert fake_garmin.paths() == [TOKEN_PATH, REGISTRATION_PATH]

    @pytest.mark.parametrize("status", [400, 401])
    def test_a_rejected_refresh_makes_no_deregistration_call(self, fake_garmin: FakeGarmin, status: int) -> None:
        fake_garmin.token = Reply(status=status, body=b'{"error": "invalid_grant"}')

        assert attempt() is DeregistrationOutcome.REFRESH_REJECTED
        assert fake_garmin.paths() == [TOKEN_PATH]

    @pytest.mark.parametrize("status", [403, 429, 500, 503])
    def test_a_refresh_failing_any_other_way_is_failed(self, fake_garmin: FakeGarmin, status: int) -> None:
        """A token-shaped body on such a status is still a failure: the status decides, not the body."""
        fake_garmin.token = Reply(status=status, body=REFRESHED_TOKEN_BODY)

        assert attempt() is DeregistrationOutcome.FAILED
        assert fake_garmin.paths() == [TOKEN_PATH]

    @pytest.mark.parametrize(
        "body",
        [b"not json", b"{}", b'{"access_token": ""}', b'{"access_token": null}', b'["access_token"]'],
    )
    def test_a_refresh_with_no_usable_access_token_is_failed(self, fake_garmin: FakeGarmin, body: bytes) -> None:
        fake_garmin.token = Reply(status=200, body=body)

        assert attempt() is DeregistrationOutcome.FAILED
        assert fake_garmin.paths() == [TOKEN_PATH]

    @pytest.mark.parametrize("held", [None, ""])
    def test_no_refresh_token_held_is_no_credential_and_no_call(
        self, fake_garmin: FakeGarmin, held: str | None
    ) -> None:
        assert (
            attempt(HeldTokens(access_token="held-access", refresh_token=held)) is DeregistrationOutcome.NO_CREDENTIAL
        )
        assert fake_garmin.calls == []

    def test_a_transport_error_at_the_token_endpoint_is_failed(self, fake_garmin: FakeGarmin) -> None:
        fake_garmin.token = Reply(hang_up=True)

        assert attempt() is DeregistrationOutcome.FAILED

    def test_a_transport_error_at_the_deregistration_is_failed(self, fake_garmin: FakeGarmin) -> None:
        fake_garmin.registration = Reply(hang_up=True)

        assert attempt() is DeregistrationOutcome.FAILED


class TestTheCall:
    def test_it_refreshes_first_even_with_an_access_token_held(self, fake_garmin: FakeGarmin) -> None:
        attempt()

        refresh, deregistration_call = fake_garmin.calls
        assert refresh.method == "POST"
        assert refresh.form["grant_type"] == ["refresh_token"]
        assert refresh.form["refresh_token"] == ["held-refresh"]
        assert deregistration_call.method == "DELETE"
        assert deregistration_call.authorization == "Bearer refreshed-access"

    def test_it_sends_the_app_credentials_with_the_refresh(self, fake_garmin: FakeGarmin) -> None:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(settings, "garmin_client_id", "the-client")
            attempt()

        assert fake_garmin.calls[0].form["client_id"] == ["the-client"]


class TestTheDeadline:
    def test_the_deadline_is_five_seconds(self) -> None:
        assert deregistration.DEADLINE_SECONDS == 5.0

    @pytest.fixture
    def short_deadline(self, monkeypatch: pytest.MonkeyPatch) -> float:
        monkeypatch.setattr(deregistration, "DEADLINE_SECONDS", 0.4)
        return 0.4

    @pytest.mark.parametrize("route", ["token", "registration"])
    def test_a_slow_answer_is_failed(self, fake_garmin: FakeGarmin, short_deadline: float, route: str) -> None:
        setattr(fake_garmin, route, Reply(status=200, body=REFRESHED_TOKEN_BODY, delay_seconds=3 * short_deadline))

        started = time.monotonic()
        outcome = attempt()

        assert outcome is DeregistrationOutcome.FAILED
        assert time.monotonic() - started < 2 * short_deadline + 0.2

    @pytest.mark.parametrize("route", ["token", "registration"])
    def test_a_trickle_of_bytes_past_the_deadline_is_failed(
        self, fake_garmin: FakeGarmin, short_deadline: float, route: str
    ) -> None:
        """Every byte arrives inside any per-phase timeout; only a total deadline sees this one."""
        setattr(fake_garmin, route, Reply(status=200, body=REFRESHED_TOKEN_BODY, trickle_seconds=0.05))

        started = time.monotonic()
        outcome = attempt()

        assert outcome is DeregistrationOutcome.FAILED
        assert time.monotonic() - started < 2 * short_deadline + 0.2

    def test_each_call_has_its_own_deadline(self, fake_garmin: FakeGarmin, short_deadline: float) -> None:
        """Two calls each just inside the deadline: the attempt spends more than one deadline."""
        fake_garmin.token = Reply(status=200, body=REFRESHED_TOKEN_BODY, delay_seconds=0.75 * short_deadline)
        fake_garmin.registration = Reply(status=204, delay_seconds=0.75 * short_deadline)

        assert attempt() is DeregistrationOutcome.CONFIRMED


class TestTheLogLine:
    @pytest.mark.parametrize(
        ("registration", "token", "expected"),
        [
            (Reply(status=204), None, "confirmed"),
            (Reply(status=404), None, "not_found"),
            (Reply(status=500), None, "failed"),
            (Reply(status=204), Reply(status=400), "refresh_rejected"),
            (Reply(status=204), Reply(status=500), "failed"),
        ],
    )
    def test_each_attempt_logs_exactly_one_line_with_its_outcome_and_trigger(
        self,
        fake_garmin: FakeGarmin,
        capsys: pytest.CaptureFixture[str],
        registration: Reply,
        token: Reply | None,
        expected: str,
    ) -> None:
        fake_garmin.registration = registration
        if token is not None:
            fake_garmin.token = token

        attempt_deregistration(
            GarminStrategy().oauth,
            HELD,
            trigger=DeregistrationTrigger.USER_DELETE,
            user_id=uuid4(),
        )

        lines = logged_lines(capsys.readouterr().out)
        assert len(lines) == 1
        assert lines[0]["outcome"] == expected
        assert lines[0]["trigger"] == "user_delete"

    def test_no_credential_logs_its_line(self, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]) -> None:
        attempt(HeldTokens(access_token=None, refresh_token=None))

        lines = logged_lines(capsys.readouterr().out)
        assert [line["outcome"] for line in lines] == ["no_credential"]

    def test_the_alert_literals(self, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]) -> None:
        """calibra-ow-deploy scripts/alerts.sh matches these two strings literally.

        A rename here is a telemetry migration: change both in step.
        """
        fake_garmin.registration = Reply(status=500)

        attempt()

        logged = capsys.readouterr().out
        assert '"action": "garmin_deregistration"' in logged
        assert '"outcome": "failed"' in logged

    def test_a_token_never_reaches_the_log(self, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]) -> None:
        fake_garmin.registration = Reply(status=500, body=b"held-refresh refreshed-access")

        attempt()

        logged = capsys.readouterr().out
        assert "held-refresh" not in logged
        assert "held-access" not in logged
        assert "refreshed-access" not in logged

    def test_held_tokens_do_not_print(self) -> None:
        assert "held-refresh" not in repr(HELD)
        assert "held-access" not in repr(HELD)
