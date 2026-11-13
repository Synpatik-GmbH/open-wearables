"""One deregistration attempt at Polar AccessLink (Calibra GL-20; the Polar rule R-2026-10-26-c)."""

import json
import time
from uuid import UUID

import pytest

from app.services.providers.garmin.deregistration import DeregistrationTrigger, HeldTokens
from app.services.providers.polar import deregistration
from app.services.providers.polar.deregistration import (
    ALERTED_OUTCOMES,
    PolarDeregistrationOutcome,
    attempt_deregistration,
)
from app.services.providers.polar.strategy import PolarStrategy
from tests.providers.garmin.conftest import Reply
from tests.providers.polar.conftest import DEREGISTRATION_PATH, POLAR_USER_ID, FakePolar

HELD = HeldTokens(access_token="held-access", refresh_token=None, provider_user_id=POLAR_USER_ID)
USER = UUID("6f1c2a3b-0000-4000-8000-000000000002")


def attempt(
    held: HeldTokens = HELD, trigger: DeregistrationTrigger = DeregistrationTrigger.DISCONNECT
) -> PolarDeregistrationOutcome:
    oauth = PolarStrategy().oauth
    assert oauth is not None
    return attempt_deregistration(oauth, held, trigger=trigger, user_id=USER)


def logged_lines(out: str) -> list[dict]:
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "polar_deregistration"]


class TestOutcome:
    @pytest.mark.parametrize("status", [200, 204])
    def test_a_success_is_confirmed(self, fake_polar: FakePolar, status: int) -> None:
        fake_polar.registration = Reply(status=status)

        assert attempt() is PolarDeregistrationOutcome.CONFIRMED
        assert [(call.method, call.path) for call in fake_polar.calls] == [("DELETE", DEREGISTRATION_PATH)]

    def test_a_404_is_not_found(self, fake_polar: FakePolar) -> None:
        fake_polar.registration = Reply(status=404)

        assert attempt() is PolarDeregistrationOutcome.NOT_FOUND

    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
    def test_any_other_status_is_failed(self, fake_polar: FakePolar, status: int) -> None:
        fake_polar.registration = Reply(status=status)

        assert attempt() is PolarDeregistrationOutcome.FAILED

    @pytest.mark.parametrize(
        ("access", "polar_user"),
        [(None, POLAR_USER_ID), ("", POLAR_USER_ID), ("held-access", None), ("held-access", ""), (None, None)],
    )
    def test_no_token_or_no_polar_user_is_no_credential_and_no_call(
        self, fake_polar: FakePolar, access: str | None, polar_user: str | None
    ) -> None:
        held = HeldTokens(access_token=access, refresh_token="held-refresh", provider_user_id=polar_user)

        assert attempt(held) is PolarDeregistrationOutcome.NO_CREDENTIAL
        assert fake_polar.calls == []

    def test_a_transport_error_is_failed(self, fake_polar: FakePolar) -> None:
        fake_polar.registration = Reply(hang_up=True)

        assert attempt() is PolarDeregistrationOutcome.FAILED

    def test_only_failed_is_alerted(self) -> None:
        assert ALERTED_OUTCOMES == (PolarDeregistrationOutcome.FAILED,)

    def test_the_outcome_literals_are_the_ones_the_alert_names(self) -> None:
        assert [outcome.value for outcome in PolarDeregistrationOutcome] == [
            "confirmed",
            "not_found",
            "no_credential",
            "failed",
        ]


class TestTheCall:
    def test_it_presents_the_held_access_token_as_a_bearer(self, fake_polar: FakePolar) -> None:
        attempt()

        (call,) = fake_polar.calls
        assert call.authorization == "Bearer held-access"

    def test_the_polar_user_id_is_escaped_into_the_path(self, fake_polar: FakePolar) -> None:
        attempt(HeldTokens(access_token="held-access", refresh_token=None, provider_user_id="a/b"))

        (call,) = fake_polar.calls
        assert call.path == "/v3/users/a%2Fb"


class TestTheDeadline:
    def test_the_deadline_is_five_seconds(self) -> None:
        assert deregistration.DEADLINE_SECONDS == 5.0

    @pytest.fixture
    def short_deadline(self, monkeypatch: pytest.MonkeyPatch) -> float:
        monkeypatch.setattr(deregistration, "DEADLINE_SECONDS", 0.4)
        return 0.4

    def test_a_slow_answer_is_failed(self, fake_polar: FakePolar, short_deadline: float) -> None:
        fake_polar.registration = Reply(status=204, delay_seconds=3 * short_deadline)

        started = time.monotonic()
        outcome = attempt()

        assert outcome is PolarDeregistrationOutcome.FAILED
        assert time.monotonic() - started < 2 * short_deadline

    def test_the_deadline_is_total_so_a_slow_trickle_is_failed(
        self, fake_polar: FakePolar, short_deadline: float
    ) -> None:
        fake_polar.registration = Reply(status=200, body=b'{"padding": "xxxxxxxxxx"}', trickle_seconds=0.1)

        started = time.monotonic()
        outcome = attempt()

        assert outcome is PolarDeregistrationOutcome.FAILED
        assert time.monotonic() - started < 2 * short_deadline


class TestTheLogLine:
    @pytest.mark.parametrize(
        ("reply", "outcome", "level"),
        [
            (Reply(status=204), "confirmed", "info"),
            (Reply(status=404), "not_found", "info"),
            (Reply(status=500), "failed", "error"),
        ],
    )
    def test_one_line_with_the_fields_for_each_outcome(
        self, fake_polar: FakePolar, capsys: pytest.CaptureFixture[str], reply: Reply, outcome: str, level: str
    ) -> None:
        fake_polar.registration = reply

        attempt(trigger=DeregistrationTrigger.USER_DELETE)

        (line,) = logged_lines(capsys.readouterr().out)
        assert line["message"] == "Polar deregistration attempt"
        assert line["level"] == level
        assert line["provider"] == "polar"
        assert line["outcome"] == outcome
        assert line["trigger"] == "user_delete"
        assert line["user_id"] == str(USER)
        assert ("cause" in line) is (outcome == "failed")

    def test_no_credential_logs_one_info_line_without_a_cause(self, capsys: pytest.CaptureFixture[str]) -> None:
        attempt(HeldTokens(access_token=None, refresh_token=None))

        (line,) = logged_lines(capsys.readouterr().out)
        assert line["outcome"] == "no_credential"
        assert line["level"] == "info"
        assert "cause" not in line

    def test_a_failed_answer_carries_its_status_as_the_cause(
        self, fake_polar: FakePolar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_polar.registration = Reply(status=503, body=b"upstream said something about held-access")

        attempt()

        (line,) = logged_lines(capsys.readouterr().out)
        assert line["cause"] == "503"
        assert "held-access" not in json.dumps(line)

    def test_a_failure_with_no_answer_carries_the_exception_class_only(
        self, fake_polar: FakePolar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_polar.registration = Reply(hang_up=True)

        attempt()

        (line,) = logged_lines(capsys.readouterr().out)
        assert line["cause"].isidentifier()

    def test_an_unexpected_error_is_failed_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def boom(*_: object, **__: object) -> None:
            raise RuntimeError("held-access leaked into a message")

        monkeypatch.setattr(deregistration, "_request", boom)

        assert attempt() is PolarDeregistrationOutcome.FAILED
        (line,) = logged_lines(capsys.readouterr().out)
        assert line["cause"] == "RuntimeError"
