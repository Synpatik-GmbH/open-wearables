"""A Polar disconnect and an account deletion each make one deregistration attempt after the end
(Calibra GL-20; the Polar rule R-2026-10-26-c, as Garmin 2.71.3.3 D-05/D-06 and Google 2.72.3 D-07/D-08).

Account deletion and consent withdrawal both reach ``user_service.delete`` through the adapter's
``DELETE /api/v1/users/{id}``. As in Garmin's and Google's tests, the ordering is about what is
committed, so these commit against the engine and read from a second session, inside the request
the fake Polar receives.
"""

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import BaseDbModel
from app.models import User, UserConnection
from app.schemas.auth import ConnectionStatus
from app.services.providers.polar.strategy import PolarStrategy
from app.services.user_connection_service import user_connection_service
from app.services.user_service import user_service
from tests.providers.garmin.conftest import Call, Reply
from tests.providers.polar.conftest import DEREGISTRATION_PATH, POLAR_USER_ID, FakePolar


def attempts_logged(out: str) -> list[dict]:
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "polar_deregistration"]


def _row_counts(session: Session) -> dict[str, int]:
    return {
        table.name: session.execute(select(func.count()).select_from(table)).scalar_one()
        for table in BaseDbModel.metadata.sorted_tables
    }


@pytest.fixture
def member_factory(session_factory: Any) -> Iterator[Any]:
    """Commits a member with one Polar row in the state asked for, and cleans up after."""
    made: list[UUID] = []
    with session_factory() as session:
        counts_before = _row_counts(session)

    def make(
        status: ConnectionStatus = ConnectionStatus.ACTIVE,
        access: str | None = "held-access",
        polar_user: str | None = POLAR_USER_ID,
    ) -> UUID:
        user_id = uuid4()
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(User(id=user_id, created_at=now, email=f"{user_id}@example.test"))
            session.flush()
            session.add(
                UserConnection(
                    id=uuid4(),
                    user_id=user_id,
                    provider="polar",
                    provider_user_id=polar_user,
                    access_token=access,
                    refresh_token=None,
                    status=status,
                    created_at=now,
                    updated_at=now,
                )
            )
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
        return reader.query(UserConnection).filter_by(user_id=user_id, provider="polar").one_or_none()


def disconnect(session_factory: Any, user_id: UUID) -> None:
    with session_factory() as session:
        user_connection_service.disconnect(session, user_id, "polar", oauth=PolarStrategy().oauth)


def delete(session_factory: Any, user_id: UUID) -> None:
    with session_factory() as session:
        user_service.delete(session, user_id)


class TestDisconnect:
    def test_the_row_is_revoked_with_no_token_and_committed_before_the_call(
        self, session_factory: Any, member_factory: Any, fake_polar: FakePolar
    ) -> None:
        user_id = member_factory()
        seen: list[tuple[str, ConnectionStatus, str | None]] = []

        def read_the_row(call: Call) -> None:
            row = stored(session_factory, user_id)
            assert row is not None
            seen.append((call.path, row.status, row.access_token))

        fake_polar.on_call = read_the_row
        disconnect(session_factory, user_id)

        assert seen == [(DEREGISTRATION_PATH, ConnectionStatus.REVOKED, None)]

    def test_the_attempt_presents_the_token_read_before_the_revoke(
        self, session_factory: Any, member_factory: Any, fake_polar: FakePolar
    ) -> None:
        disconnect(session_factory, member_factory())

        (call,) = fake_polar.calls
        assert call.authorization == "Bearer held-access"

    def test_a_retried_disconnect_makes_no_second_attempt(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_polar: FakePolar,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        user_id = member_factory()
        disconnect(session_factory, user_id)
        capsys.readouterr()

        disconnect(session_factory, user_id)

        assert len(fake_polar.calls) == 1
        assert attempts_logged(capsys.readouterr().out) == []

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [(Reply(status=204), "confirmed"), (Reply(status=404), "not_found"), (Reply(status=500), "failed")],
    )
    def test_each_outcome_is_logged_once_and_the_row_ends_revoked(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_polar: FakePolar,
        capsys: pytest.CaptureFixture[str],
        reply: Reply,
        expected: str,
    ) -> None:
        user_id = member_factory()
        fake_polar.registration = reply

        disconnect(session_factory, user_id)

        (line,) = attempts_logged(capsys.readouterr().out)
        assert (line["outcome"], line["trigger"]) == (expected, "disconnect")
        row = stored(session_factory, user_id)
        assert row is not None
        assert (row.status, row.access_token) == (ConnectionStatus.REVOKED, None)


class TestAccountDeletion:
    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            (Reply(status=204), "confirmed"),
            (Reply(status=404), "not_found"),
            (Reply(status=401), "failed"),
            (Reply(status=500), "failed"),
            (Reply(hang_up=True), "failed"),
        ],
    )
    def test_the_attempt_follows_the_delete_and_the_user_is_deleted_whatever_the_outcome(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_polar: FakePolar,
        capsys: pytest.CaptureFixture[str],
        reply: Reply,
        expected: str,
    ) -> None:
        user_id = member_factory()
        fake_polar.registration = reply
        user_present_at_call: list[bool] = []

        def read_the_user(_: Call) -> None:
            with session_factory() as reader:
                user_present_at_call.append(reader.get(User, user_id) is not None)

        fake_polar.on_call = read_the_user
        delete(session_factory, user_id)

        assert user_present_at_call == [False]
        (call,) = fake_polar.calls
        assert (call.method, call.path, call.authorization) == ("DELETE", DEREGISTRATION_PATH, "Bearer held-access")
        (line,) = attempts_logged(capsys.readouterr().out)
        assert (line["outcome"], line["trigger"]) == (expected, "user_delete")
        with session_factory() as reader:
            assert reader.get(User, user_id) is None

    def test_the_failure_is_no_longer_only_the_generic_warning(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_polar: FakePolar,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_polar.registration = Reply(status=500)

        delete(session_factory, member_factory())

        out = capsys.readouterr().out
        assert "Failed to deregister user" not in out
        (line,) = attempts_logged(out)
        assert (line["level"], line["outcome"]) == ("error", "failed")

    def test_a_revoked_row_gets_no_attempt(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_polar: FakePolar,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A revoked row was disconnected, which made its attempt then; or Polar refused its token."""
        delete(session_factory, member_factory(status=ConnectionStatus.REVOKED))

        assert fake_polar.calls == []
        assert attempts_logged(capsys.readouterr().out) == []

    def test_an_expired_row_gets_an_attempt(
        self, session_factory: Any, member_factory: Any, fake_polar: FakePolar
    ) -> None:
        delete(session_factory, member_factory(status=ConnectionStatus.EXPIRED))

        assert len(fake_polar.calls) == 1

    def test_a_row_holding_no_token_gets_no_call(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_polar: FakePolar,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        delete(session_factory, member_factory(access=None))

        assert fake_polar.calls == []
        assert attempts_logged(capsys.readouterr().out) == []

    def test_a_row_without_a_polar_user_id_logs_no_credential_and_makes_no_call(
        self,
        session_factory: Any,
        member_factory: Any,
        fake_polar: FakePolar,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        delete(session_factory, member_factory(polar_user=None))

        assert fake_polar.calls == []
        (line,) = attempts_logged(capsys.readouterr().out)
        assert line["outcome"] == "no_credential"

    def test_a_delete_that_fails_makes_no_attempt(
        self, session_factory: Any, member_factory: Any, fake_polar: FakePolar
    ) -> None:
        user_id = member_factory()
        with (
            patch.object(user_service.crud, "delete", side_effect=RuntimeError("the delete failed")),
            session_factory() as session,
            pytest.raises(RuntimeError),
        ):
            user_service.delete(session, user_id)

        assert fake_polar.calls == []
