"""A Garmin disconnect revokes first and attempts after (Calibra 2.71.3.3, design D-05; AC 14).

The ordering is about what is committed, and the ``db`` fixture's commit only releases a savepoint.
So the ordering tests commit against the engine and read from a second session, inside the request
the fake Garmin receives. The fixture deletes what it committed and checks every table holds as
many rows as before.
"""

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import BaseDbModel
from app.models import User, UserConnection
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.auth import ConnectionStatus
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.user_connection_service import user_connection_service
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.providers.garmin.conftest import REGISTRATION_PATH, TOKEN_PATH, Call, FakeGarmin, Reply
from tests.utils import api_key_headers

SERVICE_MODULE = "app.services.user_connection_service"


def _row_counts(session: Session) -> dict[str, int]:
    return {
        table.name: session.execute(select(func.count()).select_from(table)).scalar_one()
        for table in BaseDbModel.metadata.sorted_tables
    }


def attempts_logged(out: str) -> list[dict]:
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "garmin_deregistration"]


@pytest.fixture
def committed_member(session_factory: Any) -> Iterator[UUID]:
    """A member with an active Garmin connection, committed for every session to see."""
    user_id = uuid4()
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        counts_before = _row_counts(session)
        session.add(User(id=user_id, created_at=now, email=f"{user_id}@example.test"))
        session.flush()
        session.add(
            UserConnection(
                id=uuid4(),
                user_id=user_id,
                provider="garmin",
                provider_user_id=f"garmin-{user_id}",
                access_token="held-access",
                refresh_token="held-refresh",
                token_expires_at=now,
                status=ConnectionStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        WearableHistoryRepository().begin_source(session, user_id, "garmin")
        session.commit()
    try:
        yield user_id
    finally:
        with session_factory() as session:
            session.query(User).filter(User.id == user_id).delete()
            session.commit()
            assert _row_counts(session) == counts_before


def stored_connection(session_factory: Any, user_id: UUID) -> UserConnection:
    with session_factory() as reader:
        return reader.query(UserConnection).filter_by(user_id=user_id, provider="garmin").one()


class TestOrdering:
    def test_the_row_is_revoked_and_committed_before_the_first_call_to_garmin(
        self, session_factory: Any, committed_member: UUID, fake_garmin: FakeGarmin
    ) -> None:
        seen: list[tuple[str, ConnectionStatus, str | None, str | None, datetime | None]] = []

        def read_the_row(call: Call) -> None:
            row = stored_connection(session_factory, committed_member)
            seen.append((call.path, row.status, row.access_token, row.refresh_token, row.token_expires_at))

        fake_garmin.on_call = read_the_row
        with session_factory() as session:
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=GarminStrategy().oauth)

        assert seen == [
            (TOKEN_PATH, ConnectionStatus.REVOKED, None, None, None),
            (REGISTRATION_PATH, ConnectionStatus.REVOKED, None, None, None),
        ]

    def test_connection_revoked_goes_out_before_the_first_call_to_garmin(
        self, session_factory: Any, committed_member: UUID, fake_garmin: FakeGarmin
    ) -> None:
        emitted: list[int] = []

        with patch(f"{SERVICE_MODULE}.on_connection_revoked") as revoked:
            revoked.side_effect = lambda **_: emitted.append(len(fake_garmin.calls))
            with session_factory() as session:
                user_connection_service.disconnect(session, committed_member, "garmin", oauth=GarminStrategy().oauth)

        assert emitted == [0]
        assert len(fake_garmin.calls) == 2

    def test_the_attempt_uses_the_tokens_read_before_the_revoke(
        self, session_factory: Any, committed_member: UUID, fake_garmin: FakeGarmin
    ) -> None:
        with session_factory() as session:
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=GarminStrategy().oauth)

        assert fake_garmin.calls[0].form["refresh_token"] == ["held-refresh"]


class TestTheTransaction:
    def test_no_transaction_is_held_open_across_the_attempt(
        self, session_factory: Any, committed_member: UUID, fake_garmin: FakeGarmin
    ) -> None:
        """A slow Garmin must not hold a pooled connection idle in a transaction."""
        in_transaction: list[bool] = []

        with session_factory() as session:
            fake_garmin.on_call = lambda _: in_transaction.append(session.in_transaction())
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=GarminStrategy().oauth)

        assert in_transaction == [False, False]


class TestOneAttempt:
    def test_a_retried_disconnect_on_a_revoked_row_makes_no_attempt(
        self,
        session_factory: Any,
        committed_member: UUID,
        fake_garmin: FakeGarmin,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        oauth = GarminStrategy().oauth
        with session_factory() as session:
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=oauth)
        first_calls = list(fake_garmin.calls)
        capsys.readouterr()

        with session_factory() as session:
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=oauth)

        assert fake_garmin.calls == first_calls
        assert attempts_logged(capsys.readouterr().out) == []

    def test_of_two_disconnects_at_once_only_the_one_that_changed_the_row_attempts(
        self,
        session_factory: Any,
        committed_member: UUID,
        fake_garmin: FakeGarmin,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The loser read the tokens before the winner's revoke, so it holds them and must not use them."""
        oauth = GarminStrategy().oauth
        real_disconnect = user_connection_service.crud.disconnect

        def the_other_request_wins_first(db: Session, user_id: UUID, provider: str) -> int:
            with session_factory() as winner:
                assert real_disconnect(winner, user_id, provider) == 1
            return real_disconnect(db, user_id, provider)

        with (
            patch.object(user_connection_service.crud, "disconnect", side_effect=the_other_request_wins_first),
            session_factory() as loser,
        ):
            user_connection_service.disconnect(loser, committed_member, "garmin", oauth=oauth)

        assert fake_garmin.calls == []
        assert attempts_logged(capsys.readouterr().out) == []


class TestEveryOutcomeLeavesTheRowRevokedAndEmpty:
    @pytest.mark.parametrize(
        ("token", "registration", "expected"),
        [
            (None, Reply(status=204), "confirmed"),
            (None, Reply(status=404), "not_found"),
            (Reply(status=400), Reply(status=204), "refresh_rejected"),
            (None, Reply(status=500), "failed"),
            (None, Reply(hang_up=True), "failed"),
        ],
    )
    def test_each_outcome(
        self,
        session_factory: Any,
        committed_member: UUID,
        fake_garmin: FakeGarmin,
        capsys: pytest.CaptureFixture[str],
        token: Reply | None,
        registration: Reply,
        expected: str,
    ) -> None:
        if token is not None:
            fake_garmin.token = token
        fake_garmin.registration = registration

        with session_factory() as session:
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=GarminStrategy().oauth)

        row = stored_connection(session_factory, committed_member)
        assert (row.status, row.access_token, row.refresh_token, row.token_expires_at) == (
            ConnectionStatus.REVOKED,
            None,
            None,
            None,
        )
        lines = attempts_logged(capsys.readouterr().out)
        assert [(line["outcome"], line["trigger"]) for line in lines] == [(expected, "disconnect")]

    def test_no_credential_leaves_it_revoked_with_no_call(
        self,
        session_factory: Any,
        committed_member: UUID,
        fake_garmin: FakeGarmin,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with session_factory() as session:
            session.query(UserConnection).filter_by(user_id=committed_member).update({"refresh_token": None})
            session.commit()
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=GarminStrategy().oauth)

        assert fake_garmin.calls == []
        assert stored_connection(session_factory, committed_member).status == ConnectionStatus.REVOKED
        assert [line["outcome"] for line in attempts_logged(capsys.readouterr().out)] == ["no_credential"]

    def test_a_rejected_refresh_writes_nothing_to_the_row_and_emits_nothing(
        self, session_factory: Any, committed_member: UUID, fake_garmin: FakeGarmin
    ) -> None:
        """Today's refresh path revokes and emits on a rejection; this attempt must not take it."""
        fake_garmin.token = Reply(status=400)
        stored: list[datetime] = []

        def stamp(call: Call) -> None:
            stored.append(stored_connection(session_factory, committed_member).updated_at)

        fake_garmin.on_call = stamp
        with (
            patch(f"{SERVICE_MODULE}.on_connection_revoked") as ours,
            patch("app.services.providers.templates.base_oauth.on_connection_revoked") as refresh_path,
            session_factory() as session,
        ):
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=GarminStrategy().oauth)

        assert ours.call_count == 1
        refresh_path.assert_not_called()
        assert stored_connection(session_factory, committed_member).updated_at == stored[0]


class TestWhichRowsAttempt:
    def test_an_expired_row_disconnects_like_an_active_one(
        self,
        session_factory: Any,
        committed_member: UUID,
        fake_garmin: FakeGarmin,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The app offers disconnect on an expired row (D-01), and its registration still stands."""
        with session_factory() as session:
            session.query(UserConnection).filter_by(user_id=committed_member).update(
                {"status": ConnectionStatus.EXPIRED}
            )
            session.commit()
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=GarminStrategy().oauth)

        assert fake_garmin.paths() == [TOKEN_PATH, REGISTRATION_PATH]
        assert [line["outcome"] for line in attempts_logged(capsys.readouterr().out)] == ["confirmed"]

    def test_a_row_holding_only_a_refresh_token_gets_its_attempt(
        self, session_factory: Any, committed_member: UUID, fake_garmin: FakeGarmin
    ) -> None:
        """Today's path keys on the access token; the attempt needs only the refresh token."""
        with session_factory() as session:
            session.query(UserConnection).filter_by(user_id=committed_member).update({"access_token": None})
            session.commit()
            user_connection_service.disconnect(session, committed_member, "garmin", oauth=GarminStrategy().oauth)

        assert fake_garmin.paths() == [TOKEN_PATH, REGISTRATION_PATH]

    def test_another_provider_keeps_its_call_before_the_revoke(self, db: Session, fake_garmin: FakeGarmin) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar", status=ConnectionStatus.ACTIVE, access_token="polar-token")
        oauth = MagicMock()
        status_at_the_call: list[ConnectionStatus] = []

        def record(*_: Any, **__: Any) -> None:
            status_at_the_call.append(db.query(UserConnection).filter_by(user_id=user.id).one().status)

        oauth.deregister_user.side_effect = record

        user_connection_service.disconnect(db, user.id, "polar", oauth=oauth)

        assert status_at_the_call == [ConnectionStatus.ACTIVE]
        assert fake_garmin.calls == []


class TestAnotherProvider:
    """Garmin's own tests carried this until the attempt moved: the path stays for everyone else."""

    def test_a_failing_deregistration_does_not_block_a_polar_disconnect(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar", status=ConnectionStatus.ACTIVE)

        with patch(
            "app.services.providers.polar.oauth.PolarOAuth.deregister_user", side_effect=RuntimeError("polar is down")
        ) as polar:
            response = client.delete(
                f"/api/v1/users/{user.id}/connections/polar", headers=api_key_headers(ApiKeyFactory().plain_key)
            )

        polar.assert_called_once()
        assert response.status_code == 204
        row = db.query(UserConnection).filter_by(user_id=user.id, provider="polar").one()
        assert row.status == ConnectionStatus.REVOKED


class TestTheRoute:
    def test_disconnect_answers_after_the_attempt(
        self, client: TestClient, db: Session, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.ACTIVE)

        response = client.delete(
            f"/api/v1/users/{user.id}/connections/garmin", headers=api_key_headers(ApiKeyFactory().plain_key)
        )

        assert response.status_code == 204
        assert fake_garmin.paths() == [TOKEN_PATH, REGISTRATION_PATH]
        assert [line["outcome"] for line in attempts_logged(capsys.readouterr().out)] == ["confirmed"]

    def test_a_failed_attempt_still_answers_204_and_revokes(
        self, client: TestClient, db: Session, fake_garmin: FakeGarmin
    ) -> None:
        fake_garmin.registration = Reply(status=500)
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.ACTIVE)

        response = client.delete(
            f"/api/v1/users/{user.id}/connections/garmin", headers=api_key_headers(ApiKeyFactory().plain_key)
        )

        assert response.status_code == 204
        row = db.query(UserConnection).filter_by(user_id=user.id, provider="garmin").one()
        assert (row.status, row.access_token, row.refresh_token) == (ConnectionStatus.REVOKED, None, None)

    def test_the_data_purge_route_makes_the_same_attempt(
        self, client: TestClient, db: Session, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.ACTIVE)

        response = client.delete(
            f"/api/v1/users/{user.id}/connections/garmin/data", headers=api_key_headers(ApiKeyFactory().plain_key)
        )

        assert response.status_code == 204
        assert [line["trigger"] for line in attempts_logged(capsys.readouterr().out)] == ["disconnect"]
