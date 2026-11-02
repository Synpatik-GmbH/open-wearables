"""One revoke attempt at Google (Calibra 2.72.3, design D-07; spec P12 and AC 17)."""

import json
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.services.providers.garmin.deregistration import DeregistrationTrigger, HeldTokens
from app.services.providers.google_health import revocation
from app.services.providers.google_health.revocation import (
    ALERTED_OUTCOMES,
    RevokeOutcome,
    attempt_revoke,
)
from tests.providers.garmin.conftest import Reply
from tests.providers.google_health.conftest import REVOKE_PATH, FakeGoogle

HELD = HeldTokens(access_token="held-access", refresh_token="held-refresh")
USER = UUID("6f1c2a3b-0000-4000-8000-000000000001")


def attempt(
    held: HeldTokens = HELD, trigger: DeregistrationTrigger = DeregistrationTrigger.DISCONNECT
) -> RevokeOutcome:
    return attempt_revoke(held, trigger=trigger, user_id=USER)


def logged_lines(out: str) -> list[dict]:
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "google_health_revoke"]


class TestOutcome:
    def test_a_200_is_confirmed(self, fake_google: FakeGoogle) -> None:
        fake_google.revoke = Reply(status=200, body=b"{}")

        assert attempt() is RevokeOutcome.CONFIRMED
        assert [call.path for call in fake_google.calls] == [REVOKE_PATH]

    def test_a_400_invalid_token_is_already_invalid(self, fake_google: FakeGoogle) -> None:
        fake_google.revoke = Reply(status=400, body=b'{"error": "invalid_token", "error_description": "Token expired"}')

        assert attempt() is RevokeOutcome.ALREADY_INVALID

    @pytest.mark.parametrize(
        "body",
        [b'{"error": "invalid_request"}', b'{"error": "INVALID_TOKEN"}', b"{}", b"not json", b'["invalid_token"]', b""],
    )
    def test_any_other_400_is_failed(self, fake_google: FakeGoogle, body: bytes) -> None:
        fake_google.revoke = Reply(status=400, body=body)

        assert attempt() is RevokeOutcome.FAILED

    @pytest.mark.parametrize("status", [201, 204, 401, 403, 404, 429, 500, 503])
    def test_any_other_status_is_failed(self, fake_google: FakeGoogle, status: int) -> None:
        """Only 200 confirms: Google's documented success answer, nothing near it."""
        fake_google.revoke = Reply(status=status, body=b'{"error": "invalid_token"}')

        assert attempt() is RevokeOutcome.FAILED

    @pytest.mark.parametrize(("access", "refresh"), [(None, None), ("", ""), (None, ""), ("", None)])
    def test_no_token_held_is_no_credential_and_no_call(
        self, fake_google: FakeGoogle, access: str | None, refresh: str | None
    ) -> None:
        assert attempt(HeldTokens(access_token=access, refresh_token=refresh)) is RevokeOutcome.NO_CREDENTIAL
        assert fake_google.calls == []

    def test_a_transport_error_is_failed(self, fake_google: FakeGoogle) -> None:
        fake_google.revoke = Reply(hang_up=True)

        assert attempt() is RevokeOutcome.FAILED

    def test_only_failed_is_alerted(self) -> None:
        assert ALERTED_OUTCOMES == (RevokeOutcome.FAILED,)

    def test_the_outcome_literals_are_the_ones_the_alert_and_the_spec_name(self) -> None:
        assert [outcome.value for outcome in RevokeOutcome] == [
            "confirmed",
            "already_invalid",
            "no_credential",
            "failed",
        ]


class TestTheCall:
    def test_it_posts_the_refresh_token_when_one_is_held(self, fake_google: FakeGoogle) -> None:
        attempt()

        (call,) = fake_google.calls
        assert call.method == "POST"
        assert call.form == {"token": ["held-refresh"]}

    @pytest.mark.parametrize("refresh", [None, ""])
    def test_it_posts_the_access_token_when_no_refresh_token_is_held(
        self, fake_google: FakeGoogle, refresh: str | None
    ) -> None:
        attempt(HeldTokens(access_token="held-access", refresh_token=refresh))

        (call,) = fake_google.calls
        assert call.form == {"token": ["held-access"]}

    def test_it_sends_no_client_credential_and_no_refresh_comes_first(self, fake_google: FakeGoogle) -> None:
        attempt()

        (call,) = fake_google.calls
        assert call.authorization is None
        assert "client_id" not in call.form
        assert "client_secret" not in call.form

    def test_the_production_url_is_googles_revoke_endpoint(self) -> None:
        # Read off the module's source, because the fixture patches the attribute.
        assert 'REVOKE_URL = "https://oauth2.googleapis.com/revoke"' in Path(revocation.__file__).read_text()


class TestTheDeadline:
    def test_the_deadline_is_five_seconds(self) -> None:
        assert revocation.DEADLINE_SECONDS == 5.0

    @pytest.fixture
    def short_deadline(self, monkeypatch: pytest.MonkeyPatch) -> float:
        monkeypatch.setattr(revocation, "DEADLINE_SECONDS", 0.4)
        return 0.4

    def test_a_slow_answer_is_failed(self, fake_google: FakeGoogle, short_deadline: float) -> None:
        fake_google.revoke = Reply(status=200, body=b"{}", delay_seconds=3 * short_deadline)

        started = time.monotonic()
        outcome = attempt()

        assert outcome is RevokeOutcome.FAILED
        assert time.monotonic() - started < 2 * short_deadline

    def test_the_deadline_is_total_so_a_slow_trickle_is_failed(
        self, fake_google: FakeGoogle, short_deadline: float
    ) -> None:
        """Every byte arrives inside a per-read timeout, but the answer as a whole does not."""
        fake_google.revoke = Reply(status=200, body=b'{"padding": "xxxxxxxxxx"}', trickle_seconds=0.1)

        started = time.monotonic()
        outcome = attempt()

        assert outcome is RevokeOutcome.FAILED
        assert time.monotonic() - started < 2 * short_deadline


class TestTheLogLine:
    @pytest.mark.parametrize(
        ("reply", "outcome", "level"),
        [
            (Reply(status=200, body=b"{}"), "confirmed", "info"),
            (Reply(status=400, body=b'{"error": "invalid_token"}'), "already_invalid", "info"),
            (Reply(status=500, body=b"{}"), "failed", "error"),
        ],
    )
    def test_one_line_with_the_fields_for_each_outcome(
        self, fake_google: FakeGoogle, capsys: pytest.CaptureFixture[str], reply: Reply, outcome: str, level: str
    ) -> None:
        fake_google.revoke = reply

        attempt()

        (line,) = logged_lines(capsys.readouterr().out)
        assert line["message"] == "Google Health revoke attempt"
        assert line["level"] == level
        assert line["provider"] == "google_health"
        assert line["outcome"] == outcome
        assert line["trigger"] == "disconnect"
        assert line["user_id"] == str(USER)
        assert ("cause" in line) is (outcome == "failed")

    def test_no_credential_logs_one_info_line_without_a_cause(self, capsys: pytest.CaptureFixture[str]) -> None:
        attempt(HeldTokens(access_token=None, refresh_token=None))

        (line,) = logged_lines(capsys.readouterr().out)
        assert line["outcome"] == "no_credential"
        assert line["level"] == "info"
        assert "cause" not in line

    def test_a_failed_answer_carries_its_status_as_the_cause(
        self, fake_google: FakeGoogle, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_google.revoke = Reply(status=503, body=b"upstream said something about held-refresh")

        attempt()

        (line,) = logged_lines(capsys.readouterr().out)
        assert line["cause"] == "503"

    def test_a_failure_with_no_answer_carries_the_exception_class_only(
        self, fake_google: FakeGoogle, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_google.revoke = Reply(hang_up=True)

        attempt()

        (line,) = logged_lines(capsys.readouterr().out)
        assert line["cause"].isidentifier()

    @pytest.mark.parametrize(
        "reply",
        [
            Reply(status=200, body=b"{}"),
            Reply(status=400, body=b'{"error": "invalid_token", "error_description": "held-refresh is bad"}'),
            Reply(status=503, body=b"upstream said something about held-refresh"),
            Reply(hang_up=True),
        ],
    )
    def test_no_line_carries_a_token_a_body_or_exception_text(
        self, fake_google: FakeGoogle, capsys: pytest.CaptureFixture[str], reply: Reply
    ) -> None:
        fake_google.revoke = reply

        attempt()

        out = capsys.readouterr().out
        assert "held-refresh" not in out
        assert "held-access" not in out
        assert "upstream said" not in out
        assert "error_description" not in out

    def test_user_delete_is_its_trigger(self, fake_google: FakeGoogle, capsys: pytest.CaptureFixture[str]) -> None:
        attempt(trigger=DeregistrationTrigger.USER_DELETE)

        (line,) = logged_lines(capsys.readouterr().out)
        assert line["trigger"] == "user_delete"

    def test_it_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*_: object, **__: object) -> object:
            raise RuntimeError("anything at all")

        monkeypatch.setattr(revocation, "_request", explode)

        assert attempt_revoke(HELD, trigger=DeregistrationTrigger.DISCONNECT, user_id=uuid4()) is RevokeOutcome.FAILED
