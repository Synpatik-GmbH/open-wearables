"""Account deletion makes one attempt at Garmin and is never held (Calibra 2.71.3.3, D-06; AC 17)."""

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
from app.services.user_service import user_service
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.providers.garmin.conftest import REGISTRATION_PATH, TOKEN_PATH, Call, FakeGarmin, Reply
from tests.utils import api_key_headers


def attempts_logged(out: str) -> list[dict]:
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "garmin_deregistration"]


def _row_counts(session: Session) -> dict[str, int]:
    return {
        table.name: session.execute(select(func.count()).select_from(table)).scalar_one()
        for table in BaseDbModel.metadata.sorted_tables
    }


@pytest.fixture
def committed_member(session_factory: Any) -> Iterator[UUID]:
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


class TestOrdering:
    def test_the_attempt_runs_after_the_user_is_gone_with_the_tokens_read_before(
        self, session_factory: Any, committed_member: UUID, fake_garmin: FakeGarmin
    ) -> None:
        users_at_each_call: list[int] = []

        def count_the_user(_: Call) -> None:
            with session_factory() as reader:
                users_at_each_call.append(reader.query(User).filter_by(id=committed_member).count())

        fake_garmin.on_call = count_the_user
        with session_factory() as session:
            user_service.delete(session, committed_member)

        assert users_at_each_call == [0, 0]
        assert fake_garmin.calls[0].form["refresh_token"] == ["held-refresh"]

    def test_a_delete_that_fails_makes_no_attempt(
        self, session_factory: Any, committed_member: UUID, fake_garmin: FakeGarmin
    ) -> None:
        with (
            patch.object(user_service.crud, "delete", side_effect=RuntimeError("the database went away")),
            session_factory() as session,
            pytest.raises(RuntimeError),
        ):
            user_service.delete(session, committed_member)

        assert fake_garmin.calls == []


class TestEveryOutcomeCompletesTheDeletion:
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
        client: TestClient,
        db: Session,
        fake_garmin: FakeGarmin,
        capsys: pytest.CaptureFixture[str],
        token: Reply | None,
        registration: Reply,
        expected: str,
    ) -> None:
        if token is not None:
            fake_garmin.token = token
        fake_garmin.registration = registration
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.ACTIVE)

        response = client.delete(f"/api/v1/users/{user.id}", headers=api_key_headers(ApiKeyFactory().plain_key))

        assert response.status_code == 200
        assert db.query(User).filter_by(id=user.id).count() == 0
        lines = attempts_logged(capsys.readouterr().out)
        assert [(line["outcome"], line["trigger"]) for line in lines] == [(expected, "user_delete")]

    def test_no_credential_completes_it_with_no_call(
        self, db: Session, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", refresh_token=None)

        user_service.delete(db, user.id)

        assert fake_garmin.calls == []
        assert db.query(User).filter_by(id=user.id).count() == 0
        assert [line["outcome"] for line in attempts_logged(capsys.readouterr().out)] == ["no_credential"]


class TestWhichRowsAttempt:
    @pytest.mark.parametrize("status", [ConnectionStatus.ACTIVE, ConnectionStatus.EXPIRED])
    def test_a_row_that_holds_tokens_gets_its_attempt(
        self, db: Session, fake_garmin: FakeGarmin, status: ConnectionStatus
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", status=status)

        user_service.delete(db, user.id)

        assert fake_garmin.paths() == [TOKEN_PATH, REGISTRATION_PATH]

    def test_a_row_holding_only_a_refresh_token_gets_its_attempt(self, db: Session, fake_garmin: FakeGarmin) -> None:
        """Today's loop keys on the access token; the attempt needs only the refresh token."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", access_token=None)

        user_service.delete(db, user.id)

        assert fake_garmin.paths() == [TOKEN_PATH, REGISTRATION_PATH]

    def test_a_revoked_row_holds_no_token_and_gets_no_attempt(
        self, db: Session, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.REVOKED,
            access_token=None,
            refresh_token=None,
            token_expires_at=None,
        )

        user_service.delete(db, user.id)

        assert fake_garmin.calls == []
        assert attempts_logged(capsys.readouterr().out) == []
        assert db.query(User).filter_by(id=user.id).count() == 0

    def test_a_polar_failure_stays_best_effort(
        self, db: Session, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        UserConnectionFactory(user=user, provider="garmin")

        with patch(
            "app.services.providers.polar.oauth.PolarOAuth.deregister_user", side_effect=RuntimeError("polar is down")
        ) as polar:
            user_service.delete(db, user.id)

        polar.assert_called_once()
        assert db.query(User).filter_by(id=user.id).count() == 0
        assert [line["outcome"] for line in attempts_logged(capsys.readouterr().out)] == ["confirmed"]


def test_every_attempt_goes_to_garmin_only_once_per_row(
    db: Session, fake_garmin: FakeGarmin, capsys: pytest.CaptureFixture[str]
) -> None:
    """The generic loop must not also take the row the attempt covers."""
    user = UserFactory()
    UserConnectionFactory(user=user, provider="garmin")

    user_service.delete(db, user.id)

    assert fake_garmin.paths().count(REGISTRATION_PATH) == 1
    assert "Deregistering not supported" not in capsys.readouterr().out
    assert db.query(UserConnection).filter_by(user_id=user.id).count() == 0
