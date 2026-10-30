"""Garmin's tokens are refreshed before the refresh token expires (Calibra 2.71.3.3, design D-15).

Spec 2.71.3 v0.8 P16 and AC 23. Garmin's refresh token lasts about 90 days and every refresh
returns a new one, so without this job the one deregistration attempt is refused after 90 days.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.integrations.celery import tasks
from app.integrations.celery.core import create_celery
from app.integrations.celery.tasks.garmin.token_keepalive_task import keep_garmin_tokens_alive
from app.integrations.redis_client import get_redis_client
from app.models import UserConnection
from app.schemas.auth import ConnectionStatus
from app.services.providers.garmin import token_keepalive
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.garmin.token_keepalive import ACTION, MARGIN, keep_tokens_alive
from app.services.user_connection_service import user_connection_service
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.garmin.conftest import REGISTRATION_PATH, TOKEN_PATH, FakeGarmin, Reply

NOW = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)
LIFETIME_SECONDS = 7775998
ROTATED_BODY = json.dumps(
    {
        "access_token": "new-access",
        "token_type": "bearer",
        "expires_in": 86400,
        "refresh_token": "new-refresh",
        "refresh_token_expires_in": LIFETIME_SECONDS,
    }
).encode()
EMITTER = "app.services.providers.templates.base_oauth.on_connection_revoked"


def connection(**overrides: Any) -> UserConnection:
    values: dict[str, Any] = {
        "provider": "garmin",
        "status": ConnectionStatus.ACTIVE,
        "refresh_token_expires_at": NOW + timedelta(days=10),
    }
    return UserConnectionFactory(user=UserFactory(), **{**values, **overrides})


def tokens_of(row: UserConnection) -> tuple[Any, ...]:
    return row.access_token, row.refresh_token, row.token_expires_at, row.refresh_token_expires_at


def lines(out: str) -> list[dict]:
    parsed = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in parsed if line.get("action") == ACTION]


@pytest.fixture
def rotating(fake_garmin: FakeGarmin) -> FakeGarmin:
    fake_garmin.token = Reply(body=ROTATED_BODY)
    return fake_garmin


class TestWhichRowsAreDue:
    """The predicate, one case per term and both sides of the boundary."""

    def test_the_margin_is_thirty_days(self) -> None:
        assert timedelta(days=30) == MARGIN

    @pytest.mark.parametrize(
        ("overrides", "due"),
        [
            pytest.param({"refresh_token_expires_at": NOW + MARGIN}, True, id="exactly-30-days-left"),
            pytest.param({"refresh_token_expires_at": NOW + MARGIN + timedelta(seconds=1)}, False, id="a-second-more"),
            pytest.param({"refresh_token_expires_at": NOW - timedelta(days=1)}, True, id="already-past"),
            pytest.param({"refresh_token_expires_at": None}, True, id="expiry-unknown"),
            pytest.param({"status": ConnectionStatus.EXPIRED}, True, id="expired-still-earns-an-attempt"),
            pytest.param({"status": ConnectionStatus.REVOKED}, False, id="revoked"),
            pytest.param({"refresh_token": None}, False, id="no-refresh-token"),
            pytest.param({"refresh_token": ""}, False, id="empty-refresh-token"),
            pytest.param({"provider": "polar"}, False, id="another-provider"),
            pytest.param(
                {"token_expires_at": NOW + timedelta(days=1)}, True, id="a-valid-access-token-changes-nothing"
            ),
        ],
    )
    def test_a_row_is_due_iff_the_predicate_holds(
        self, db: Session, rotating: FakeGarmin, overrides: dict[str, Any], due: bool
    ) -> None:
        connection(**overrides)

        keep_tokens_alive(db, now=NOW)

        assert rotating.paths() == ([TOKEN_PATH] if due else [])


class TestARefreshedRow:
    def test_the_new_pair_and_both_expiries_are_stored(self, db: Session, rotating: FakeGarmin) -> None:
        row = connection(refresh_token="old-refresh")

        counts = keep_tokens_alive(db, now=NOW)

        db.refresh(row)
        assert rotating.calls[0].form["refresh_token"] == ["old-refresh"]
        assert (row.access_token, row.refresh_token) == ("new-access", "new-refresh")
        assert row.refresh_token_expires_at > datetime.now(timezone.utc) + timedelta(days=89)
        assert row.token_expires_at > datetime.now(timezone.utc) + timedelta(hours=23)
        assert counts == {"refreshed": 1}

    def test_it_is_not_due_the_next_day(self, db: Session, rotating: FakeGarmin) -> None:
        connection()

        keep_tokens_alive(db, now=NOW)
        keep_tokens_alive(db, now=NOW)

        assert rotating.paths() == [TOKEN_PATH]

    def test_it_logs_one_info_line_with_no_token(
        self, db: Session, rotating: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        row = connection()

        keep_tokens_alive(db, now=NOW)

        out = capsys.readouterr().out
        assert [(line["outcome"], line["level"], line["user_id"]) for line in lines(out)] == [
            ("refreshed", "info", str(row.user_id))
        ]
        assert "new-refresh" not in out
        assert "new-access" not in out

    def test_an_answer_with_no_usable_expiry_stores_the_pair_and_leaves_the_row_due(
        self, db: Session, rotating: FakeGarmin
    ) -> None:
        rotating.token = Reply(
            body=b'{"access_token": "a", "token_type": "bearer", "expires_in": 86400, "refresh_token": "r",'
            b' "refresh_token_expires_in": "soon"}'
        )
        row = connection()

        keep_tokens_alive(db, now=NOW)

        db.refresh(row)
        assert row.refresh_token == "r"
        assert row.refresh_token_expires_at is None

    def test_a_disconnect_afterwards_presents_the_new_refresh_token(self, db: Session, rotating: FakeGarmin) -> None:
        """AC 23: a connection past 90 days still makes its attempt, with a token Garmin accepts."""
        row = connection(refresh_token="old-refresh")
        keep_tokens_alive(db, now=NOW)
        rotating.calls.clear()

        with patch("app.services.user_connection_service.on_connection_revoked"):
            user_connection_service.disconnect(db, row.user_id, "garmin", oauth=GarminStrategy().oauth)

        assert rotating.paths() == [TOKEN_PATH, REGISTRATION_PATH]
        assert rotating.calls[0].form["refresh_token"] == ["new-refresh"]

    def test_no_transaction_stays_open_across_the_call(self, db: Session, rotating: FakeGarmin) -> None:
        """Design D-15, as D-05: a slow Garmin must not hold a pooled connection idle in a transaction."""
        connection()
        commits_at_the_call: list[int] = []

        with patch.object(db, "commit", wraps=db.commit) as commit:
            rotating.on_call = lambda _: commits_at_the_call.append(commit.call_count)
            keep_tokens_alive(db, now=NOW)

        assert commits_at_the_call == [1]


class TestAFailedRow:
    @pytest.mark.parametrize(
        ("reply", "outcome", "cause"),
        [
            pytest.param(Reply(status=400), "refresh_rejected", None, id="400"),
            pytest.param(Reply(status=401), "refresh_rejected", None, id="401"),
            pytest.param(Reply(status=500), "failed", "refresh_status_500", id="500"),
            pytest.param(Reply(hang_up=True), "failed", None, id="transport-error"),
            pytest.param(Reply(body=b"not json"), "failed", "JSONDecodeError", id="unreadable-answer"),
            pytest.param(
                Reply(body=b'{"access_token": "a", "token_type": "bearer", "expires_in": 86400}'),
                "failed",
                "refresh_no_refresh_token",
                id="no-refresh-token-in-the-answer",
            ),
        ],
    )
    def test_nothing_changes_and_the_outcome_is_logged_as_an_error(
        self,
        db: Session,
        fake_garmin: FakeGarmin,
        capsys: pytest.CaptureFixture[str],
        reply: Reply,
        outcome: str,
        cause: str | None,
    ) -> None:
        fake_garmin.token = reply
        row = connection()
        before = tokens_of(row)

        with patch(EMITTER) as emitted:
            counts = keep_tokens_alive(db, now=NOW)

        db.refresh(row)
        assert tokens_of(row) == before
        assert row.status == ConnectionStatus.ACTIVE
        emitted.assert_not_called()
        assert counts == {outcome: 1}
        out = capsys.readouterr().out
        # calibra-ow-deploy scripts/alerts.sh matches these two strings: a rename is a telemetry migration.
        assert '"action": "garmin_token_keepalive"' in out
        assert f'"outcome": "{outcome}"' in out
        [line] = lines(out)
        assert (line["outcome"], line["level"]) == (outcome, "error")
        if cause:
            assert line["cause"] == cause

    def test_a_rejected_row_stays_due(self, db: Session, fake_garmin: FakeGarmin) -> None:
        fake_garmin.token = Reply(status=400)
        connection()

        keep_tokens_alive(db, now=NOW)
        keep_tokens_alive(db, now=NOW)

        assert fake_garmin.paths() == [TOKEN_PATH, TOKEN_PATH]

    def test_a_slow_token_endpoint_fails_at_the_deadline(
        self, db: Session, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_garmin.token = Reply(delay_seconds=0.5)
        connection()

        with patch("app.services.providers.garmin.deregistration.DEADLINE_SECONDS", 0.1):
            keep_tokens_alive(db, now=NOW)

        assert [line["outcome"] for line in lines(capsys.readouterr().out)] == ["failed"]

    def test_one_rows_failure_leaves_the_next_row_refreshed(self, db: Session, rotating: FakeGarmin) -> None:
        rejected, refreshed = connection(refresh_token="dead"), connection(refresh_token="alive")

        rotating.token_for["dead"] = Reply(status=400)
        counts = keep_tokens_alive(db, now=NOW)

        db.refresh(rejected)
        db.refresh(refreshed)
        assert (rejected.refresh_token, refreshed.refresh_token) == ("dead", "new-refresh")
        assert counts == {"refresh_rejected": 1, "refreshed": 1}

    def test_a_failed_write_leaves_the_next_row_refreshed(self, db: Session, rotating: FakeGarmin) -> None:
        connection(), connection()
        repository = token_keepalive.connection_repository
        original = repository.store_refreshed_tokens
        attempts: list[int] = []

        def fail_the_first(*args: Any, **kwargs: Any) -> bool:
            if not attempts:
                attempts.append(1)
                db.execute(text("SELECT 1/0"))  # a real failed statement, so a missed rollback shows
            return original(*args, **kwargs)

        with patch.object(repository, "store_refreshed_tokens", side_effect=fail_the_first):
            counts = keep_tokens_alive(db, now=NOW)

        assert counts == {"failed": 1, "refreshed": 1}

    def test_a_redis_error_is_that_rows_failure(
        self, db: Session, rotating: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        connection()

        with patch("app.services.providers.garmin.token_keepalive.get_redis_client", side_effect=ConnectionError):
            counts = keep_tokens_alive(db, now=NOW)

        assert counts == {"failed": 1}
        assert rotating.paths() == []
        assert [line["cause"] for line in lines(capsys.readouterr().out)] == ["ConnectionError"]


class TestARowThatChangesDuringTheCall:
    """INV-10: the pair lands only on the row it was read from, still holding what was presented."""

    def test_a_row_revoked_during_the_call_holds_no_token_and_nothing_is_stored(
        self, db: Session, rotating: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        row = connection()
        rotating.on_call = lambda _: _revoke(db, row.id)

        counts = keep_tokens_alive(db, now=NOW)

        db.refresh(row)
        assert tokens_of(row) == (None, None, None, None)
        assert row.status == ConnectionStatus.REVOKED
        assert counts == {"superseded": 1}
        [line] = lines(capsys.readouterr().out)
        assert (line["outcome"], line["level"]) == ("superseded", "info")

    def test_a_row_reconnected_during_the_call_keeps_the_newer_tokens(self, db: Session, rotating: FakeGarmin) -> None:
        row = connection(refresh_token="old-refresh")
        rotating.on_call = lambda _: _rewrite_refresh_token(db, row.id, "reconnected")

        counts = keep_tokens_alive(db, now=NOW)

        db.refresh(row)
        assert row.refresh_token == "reconnected"
        assert counts == {"superseded": 1}


class TestTheLock:
    def test_a_held_lock_calls_nothing_and_leaves_the_row_due(
        self, db: Session, rotating: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        row = connection()
        held = get_redis_client().lock(f"token_refresh_lock:garmin:{row.user_id}", timeout=60)
        assert held.acquire(blocking=False)

        counts = keep_tokens_alive(db, now=NOW)

        assert rotating.paths() == []
        assert counts == {"locked": 1}
        assert [(line["outcome"], line["level"]) for line in lines(capsys.readouterr().out)] == [("locked", "info")]

    def test_the_lock_is_the_one_the_other_refresh_takes_and_it_is_released(
        self, db: Session, rotating: FakeGarmin
    ) -> None:
        row = connection()
        key = f"token_refresh_lock:garmin:{row.user_id}"
        seen: list[bool] = []
        rotating.on_call = lambda _: seen.append(bool(get_redis_client().exists(key)))

        keep_tokens_alive(db, now=NOW)

        assert seen == [True]
        assert not get_redis_client().exists(key)


class TestItNeverEndsAConnection:
    def test_no_consent_or_connection_event_for_any_outcome(self, db: Session, rotating: FakeGarmin) -> None:
        connection()

        with patch(EMITTER) as emitted, patch("app.services.user_connection_service.on_connection_revoked") as other:
            keep_tokens_alive(db, now=NOW)

        emitted.assert_not_called()
        other.assert_not_called()


class TestTheTask:
    def test_the_worker_loads_it(self) -> None:
        """Workers register what the tasks package imports, not what a test module imports."""
        assert tasks.keep_garmin_tokens_alive is keep_garmin_tokens_alive
        assert "keep_garmin_tokens_alive" in tasks.__all__

    def test_it_is_on_the_beat_daily(self) -> None:
        entry = create_celery().conf.beat_schedule["garmin-token-keepalive"]

        assert entry["task"] == "app.integrations.celery.tasks.garmin.token_keepalive_task.keep_garmin_tokens_alive"
        assert (entry["schedule"].hour, entry["schedule"].minute) == ({4}, {0})

    def test_it_runs_the_sweep_in_its_own_session(self, db: Session, rotating: FakeGarmin) -> None:
        row = connection(refresh_token_expires_at=None)
        session = MagicMock()
        session.__enter__.return_value = db

        with patch("app.integrations.celery.tasks.garmin.token_keepalive_task.SessionLocal", return_value=session):
            counts = keep_garmin_tokens_alive()

        db.refresh(row)
        assert row.refresh_token == "new-refresh"
        assert counts == {"refreshed": 1}


def _revoke(session: Session, row_id: Any) -> None:
    session.query(UserConnection).filter_by(id=row_id).update(
        {
            "status": ConnectionStatus.REVOKED,
            "access_token": None,
            "refresh_token": None,
            "token_expires_at": None,
            "refresh_token_expires_at": None,
        }
    )
    session.flush()


def _rewrite_refresh_token(session: Session, row_id: Any, token: str) -> None:
    session.query(UserConnection).filter_by(id=row_id).update({"refresh_token": token})
    session.flush()
