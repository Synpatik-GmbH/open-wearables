"""A Google Health disconnect and an account deletion each make one revoke attempt after the end
(Calibra 2.72.3, design D-07 and D-08; spec P12 and AC 17).

As in Garmin's tests, the ordering is about what is committed, and the ``db`` fixture's commit
only releases a savepoint. So these commit against the engine and read from a second session,
inside the request the fake Google receives, and check every table holds as many rows after.
"""

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import BaseDbModel
from app.models import User, UserConnection
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.auth import ConnectionStatus
from app.services.providers.google_health.strategy import GoogleHealthStrategy
from app.services.user_connection_service import user_connection_service
from app.services.user_service import user_service
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.providers.garmin.conftest import Call, Reply
from tests.providers.google_health.conftest import REVOKE_PATH, FakeGoogle
from tests.utils import api_key_headers

SERVICE_MODULE = "app.services.user_connection_service"


def attempts_logged(out: str) -> list[dict]:
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "google_health_revoke"]


def _row_counts(session: Session) -> dict[str, int]:
    return {
        table.name: session.execute(select(func.count()).select_from(table)).scalar_one()
        for table in BaseDbModel.metadata.sorted_tables
    }


def _connection(user_id: UUID, *, status: ConnectionStatus, access: str | None, refresh: str | None) -> UserConnection:
    now = datetime.now(timezone.utc)
    return UserConnection(
        id=uuid4(),
        user_id=user_id,
        provider="google_health",
        provider_user_id=f"google-{user_id}",
        access_token=access,
        refresh_token=refresh,
        token_expires_at=now,
        status=status,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def member_factory(session_factory: Any) -> Iterator[Any]:
    """Commits a member with one Google Health row in the state asked for, and cleans up after."""
    made: list[UUID] = []
    with session_factory() as session:
        counts_before = _row_counts(session)

    def make(
        status: ConnectionStatus = ConnectionStatus.ACTIVE,
        access: str | None = "held-access",
        refresh: str | None = "held-refresh",
    ) -> UUID:
        user_id = uuid4()
        with session_factory() as session:
            session.add(User(id=user_id, created_at=datetime.now(timezone.utc), email=f"{user_id}@example.test"))
            session.flush()
            session.add(_connection(user_id, status=status, access=access, refresh=refresh))
            WearableHistoryRepository().begin_source(session, user_id, "google_health")
            session.commit()
        made.append(user_id)
        return user_id

    yield make
    with session_factory() as session:
        session.query(User).filter(User.id.in_(made)).delete()
        session.commit()
        assert _row_counts(session) == counts_before


def stored(session_factory: Any, user_id: UUID) -> UserConnection | None:
    with session_factory() as reader:
        return reader.query(UserConnection).filter_by(user_id=user_id, provider="google_health").one_or_none()


def disconnect(session_factory: Any, user_id: UUID) -> None:
    with session_factory() as session:
        user_connection_service.disconnect(session, user_id, "google_health", oauth=GoogleHealthStrategy().oauth)


class TestDisconnectOrdering:
    def test_the_row_is_revoked_with_no_tokens_and_committed_before_the_call(
        self, session_factory: Any, member_factory: Any, fake_google: FakeGoogle
    ) -> None:
        user_id = member_factory()
        seen: list[tuple[str, ConnectionStatus, str | None, str | None, datetime | None]] = []

        def read_the_row(call: Call) -> None:
            row = stored(session_factory, user_id)
            assert row is not None
            seen.append((call.path, row.status, row.access_token, row.refresh_token, row.token_expires_at))

        fake_google.on_call = read_the_row  # type: ignore[assignment]
        disconnect(session_factory, user_id)

        assert seen == [(REVOKE_PATH, ConnectionStatus.REVOKED, None, None, None)]

    def test_connection_revoked_goes_out_before_the_call(
        self, session_factory: Any, member_factory: Any, fake_google: FakeGoogle
    ) -> None:
        user_id = member_factory()
        emitted: list[int] = []

        with patch(f"{SERVICE_MODULE}.on_connection_revoked") as revoked:
            revoked.side_effect = lambda **_: emitted.append(len(fake_google.calls))
            disconnect(session_factory, user_id)

        assert emitted == [0]
        assert len(fake_google.calls) == 1

    def test_the_attempt_presents_the_refresh_token_read_before_the_revoke(
        self, session_factory: Any, member_factory: Any, fake_google: FakeGoogle
    ) -> None:
        disconnect(session_factory, member_factory())

        assert fake_google.calls[0].form == {"token": ["held-refresh"]}

    def test_no_transaction_is_held_open_across_the_attempt(
        self, session_factory: Any, member_factory: Any, fake_google: FakeGoogle
    ) -> None:
        user_id = member_factory()
        in_transaction: list[bool] = []

        with session_factory() as session:
            fake_google.on_call = lambda _: in_transaction.append(session.in_transaction())  # type: ignore[assignment]
            user_connection_service.disconnect(session, user_id, "google_health", oauth=GoogleHealthStrategy().oauth)

        assert in_transaction == [False]

    def test_the_inherited_no_op_deregistration_is_not_logged_as_one(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_google: FakeGoogle,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        disconnect(session_factory, member_factory())

        out = capsys.readouterr().out
        assert "Deregistered user from provider API" not in out
        assert len(attempts_logged(out)) == 1


class TestOneAttempt:
    def test_a_retried_disconnect_makes_no_second_attempt(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_google: FakeGoogle,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        user_id = member_factory()
        disconnect(session_factory, user_id)
        capsys.readouterr()

        disconnect(session_factory, user_id)

        assert len(fake_google.calls) == 1
        assert attempts_logged(capsys.readouterr().out) == []

    def test_of_two_disconnects_at_once_only_the_one_that_changed_the_row_attempts(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_google: FakeGoogle,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        user_id = member_factory()
        real_disconnect = user_connection_service.crud.disconnect

        def the_other_request_wins_first(db: Session, uid: UUID, provider: str) -> int:
            with session_factory() as winner:
                assert real_disconnect(winner, uid, provider) == 1
            return real_disconnect(db, uid, provider)

        with patch.object(user_connection_service.crud, "disconnect", side_effect=the_other_request_wins_first):
            disconnect(session_factory, user_id)

        assert fake_google.calls == []
        assert attempts_logged(capsys.readouterr().out) == []

    def test_a_row_revoked_before_this_build_with_its_tokens_kept_makes_no_attempt(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_google: FakeGoogle,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        disconnect(session_factory, member_factory(status=ConnectionStatus.REVOKED))

        assert fake_google.calls == []
        assert attempts_logged(capsys.readouterr().out) == []


class TestEveryOutcomeLeavesTheRowRevokedAndEmpty:
    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            (Reply(status=200, body=b"{}"), "confirmed"),
            (Reply(status=400, body=b'{"error": "invalid_token"}'), "already_invalid"),
            (Reply(status=500, body=b"{}"), "failed"),
            (Reply(hang_up=True), "failed"),
        ],
    )
    def test_each_outcome(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_google: FakeGoogle,
        capsys: pytest.CaptureFixture[str],
        reply: Reply,
        expected: str,
    ) -> None:
        user_id = member_factory()
        fake_google.revoke = reply

        disconnect(session_factory, user_id)

        (line,) = attempts_logged(capsys.readouterr().out)
        assert line["outcome"] == expected
        assert line["trigger"] == "disconnect"
        row = stored(session_factory, user_id)
        assert row is not None
        assert (row.status, row.access_token, row.refresh_token) == (ConnectionStatus.REVOKED, None, None)


class TestAccountDeletion:
    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            (Reply(status=200, body=b"{}"), "confirmed"),
            (Reply(status=400, body=b'{"error": "invalid_token"}'), "already_invalid"),
            (Reply(status=500, body=b"{}"), "failed"),
            (Reply(hang_up=True), "failed"),
        ],
    )
    def test_the_attempt_follows_the_delete_and_the_user_is_deleted_whatever_the_outcome(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_google: FakeGoogle,
        capsys: pytest.CaptureFixture[str],
        reply: Reply,
        expected: str,
    ) -> None:
        user_id = member_factory()
        fake_google.revoke = reply
        user_present_at_call: list[bool] = []

        def read_the_user(_: Call) -> None:
            with session_factory() as reader:
                user_present_at_call.append(reader.get(User, user_id) is not None)

        fake_google.on_call = read_the_user  # type: ignore[assignment]
        with session_factory() as session:
            user_service.delete(session, user_id)

        assert user_present_at_call == [False]
        assert fake_google.calls[0].form == {"token": ["held-refresh"]}
        (line,) = attempts_logged(capsys.readouterr().out)
        assert (line["outcome"], line["trigger"]) == (expected, "user_delete")
        with session_factory() as reader:
            assert reader.get(User, user_id) is None

    def test_a_revoked_row_holding_tokens_gets_no_attempt(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_google: FakeGoogle,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A row revoked before this build kept its tokens (D-08): its grant was already ended or rejected."""
        user_id = member_factory(status=ConnectionStatus.REVOKED)
        with session_factory() as session:
            user_service.delete(session, user_id)

        assert fake_google.calls == []
        assert attempts_logged(capsys.readouterr().out) == []

    def test_an_expired_row_gets_an_attempt(
        self, session_factory: Any, member_factory: Any, fake_google: FakeGoogle
    ) -> None:
        user_id = member_factory(status=ConnectionStatus.EXPIRED)
        with session_factory() as session:
            user_service.delete(session, user_id)

        assert len(fake_google.calls) == 1

    def test_a_row_holding_no_token_gets_no_call(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_google: FakeGoogle,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        user_id = member_factory(access=None, refresh=None)
        with session_factory() as session:
            user_service.delete(session, user_id)

        assert fake_google.calls == []
        assert attempts_logged(capsys.readouterr().out) == []

    def test_a_delete_that_fails_makes_no_attempt(
        self, session_factory: Any, member_factory: Any, fake_google: FakeGoogle
    ) -> None:
        user_id = member_factory()
        with (
            patch.object(user_service.crud, "delete", side_effect=RuntimeError("the delete failed")),
            session_factory() as session,
            pytest.raises(RuntimeError),
        ):
            user_service.delete(session, user_id)

        assert fake_google.calls == []


class TestTheRoute:
    def test_disconnect_answers_204_after_one_revoke_attempt(
        self, client: TestClient, db: Session, fake_google: FakeGoogle, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="google_health", status=ConnectionStatus.ACTIVE)

        response = client.delete(
            f"/api/v1/users/{user.id}/connections/google_health", headers=api_key_headers(ApiKeyFactory().plain_key)
        )

        assert response.status_code == 204
        assert [call.path for call in fake_google.calls] == [REVOKE_PATH]
        assert [line["outcome"] for line in attempts_logged(capsys.readouterr().out)] == ["confirmed"]

    def test_a_failed_attempt_still_answers_204_and_revokes(
        self, client: TestClient, db: Session, fake_google: FakeGoogle
    ) -> None:
        fake_google.revoke = Reply(status=500, body=b"{}")
        user = UserFactory()
        UserConnectionFactory(user=user, provider="google_health", status=ConnectionStatus.ACTIVE)

        response = client.delete(
            f"/api/v1/users/{user.id}/connections/google_health", headers=api_key_headers(ApiKeyFactory().plain_key)
        )

        assert response.status_code == 204
        row = db.query(UserConnection).filter_by(user_id=user.id, provider="google_health").one()
        assert (row.status, row.access_token, row.refresh_token) == (ConnectionStatus.REVOKED, None, None)
