"""Garmin-side ends of a connection (Calibra 2.71.3.3, design D-05 and D-07; spec P14, AC 15)."""

import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import UserConnection
from app.schemas.auth import ConnectionStatus
from app.services.providers.garmin.strategy import GarminStrategy
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.garmin.conftest import REGISTRATION_PATH, TOKEN_PATH, FakeGarmin, Reply

GARMIN_USER = "garmin-shared-account"
LIFECYCLE_MODULE = "app.services.providers.garmin.handlers.lifecycle"
SERVICE_MODULE = "app.services.user_connection_service"


def push(db: Session, payload: dict) -> dict:
    return GarminStrategy().webhooks.process_payload(db, payload, "trace-lifecycle")


def attempts_logged(out: str) -> list[dict]:
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "garmin_deregistration"]


def connected(status: ConnectionStatus = ConnectionStatus.ACTIVE, garmin_user: str = GARMIN_USER) -> UserConnection:
    return UserConnectionFactory(
        user=UserFactory(),
        provider="garmin",
        provider_user_id=garmin_user,
        status=status,
        scope="ACTIVITY_EXPORT HEALTH_EXPORT",
    )


def assert_ended(db: Session, connection: UserConnection) -> None:
    db.refresh(connection)
    assert (connection.status, connection.access_token, connection.refresh_token, connection.token_expires_at) == (
        ConnectionStatus.REVOKED,
        None,
        None,
        None,
    )


class TestDeregistrationPush:
    def test_every_matching_active_row_is_revoked_and_loses_its_tokens(self, db: Session) -> None:
        first, second = connected(), connected()
        other_account = connected(garmin_user="another-account")

        result = push(db, {"deregistrations": [{"userId": GARMIN_USER}]})

        assert result["deregistrations"]["revoked"] == 2
        assert_ended(db, first)
        assert_ended(db, second)
        db.refresh(other_account)
        assert other_account.status == ConnectionStatus.ACTIVE
        assert other_account.refresh_token is not None

    def test_each_row_emits_connection_revoked_with_reason_deregistration(self, db: Session) -> None:
        first, second = connected(), connected()

        with patch(f"{LIFECYCLE_MODULE}.on_connection_revoked") as revoked:
            push(db, {"deregistrations": [{"userId": GARMIN_USER}]})

        assert {call.kwargs["user_id"] for call in revoked.call_args_list} == {first.user_id, second.user_id}
        assert {call.kwargs["reason"] for call in revoked.call_args_list} == {"deregistration"}

    def test_no_call_goes_to_garmin(self, db: Session, fake_garmin: FakeGarmin) -> None:
        """Garmin has already ended the registration."""
        connected()

        push(db, {"deregistrations": [{"userId": GARMIN_USER}]})

        assert fake_garmin.calls == []


class TestPermissionChangePush:
    @pytest.mark.parametrize("permissions", [["ACTIVITY_EXPORT"], [], ["ACTIVITY_EXPORT", "WORKOUT_IMPORT"]])
    def test_losing_health_ends_every_matching_row_with_an_attempt(
        self,
        db: Session,
        fake_garmin: FakeGarmin,
        capsys: pytest.CaptureFixture[str],
        permissions: list[str],
    ) -> None:
        first, second = connected(), connected()

        with patch(f"{SERVICE_MODULE}.on_connection_revoked") as revoked:
            push(db, {"userPermissionsChange": [{"userId": GARMIN_USER, "permissions": permissions}]})

        assert_ended(db, first)
        assert_ended(db, second)
        assert fake_garmin.paths() == [TOKEN_PATH, REGISTRATION_PATH] * 2
        assert {call.kwargs["reason"] for call in revoked.call_args_list} == {"permission_change"}
        lines = attempts_logged(capsys.readouterr().out)
        assert [line["trigger"] for line in lines] == ["permission_change", "permission_change"]

    def test_a_row_that_ends_still_takes_the_new_scope(self, db: Session, fake_garmin: FakeGarmin) -> None:
        first = connected()

        push(db, {"userPermissionsChange": [{"userId": GARMIN_USER, "permissions": ["ACTIVITY_EXPORT"]}]})

        db.refresh(first)
        assert first.scope == "ACTIVITY_EXPORT"

    def test_a_failed_attempt_still_ends_the_row(self, db: Session, fake_garmin: FakeGarmin) -> None:
        fake_garmin.registration = Reply(status=500)
        first = connected()

        push(db, {"userPermissionsChange": [{"userId": GARMIN_USER, "permissions": []}]})

        assert_ended(db, first)

    @pytest.mark.parametrize(
        ("permissions", "scope"),
        [
            (["HEALTH_EXPORT"], "HEALTH_EXPORT"),
            (["ACTIVITY_EXPORT", "HEALTH_EXPORT"], "ACTIVITY_EXPORT HEALTH_EXPORT"),
            (["HEALTH_EXPORT", "WOMEN_HEALTH_EXPORT"], "HEALTH_EXPORT WOMEN_HEALTH_EXPORT"),
        ],
    )
    def test_keeping_health_keeps_the_connection(
        self, db: Session, fake_garmin: FakeGarmin, permissions: list[str], scope: str
    ) -> None:
        first, second = connected(), connected()

        push(db, {"userPermissionsChange": [{"userId": GARMIN_USER, "permissions": permissions}]})

        for connection in (first, second):
            db.refresh(connection)
            assert connection.status == ConnectionStatus.ACTIVE
            assert connection.refresh_token is not None
            assert connection.scope == scope
        assert fake_garmin.calls == []

    @pytest.mark.parametrize("entry", [{}, {"permissions": None}, {"permissions": "ACTIVITY_EXPORT"}])
    def test_a_permissions_field_that_is_not_a_list_ends_nothing(
        self, db: Session, fake_garmin: FakeGarmin, entry: dict
    ) -> None:
        first = connected()

        push(db, {"userPermissionsChange": [{"userId": GARMIN_USER, **entry}]})

        db.refresh(first)
        assert first.status == ConnectionStatus.ACTIVE
        assert fake_garmin.calls == []

    def test_a_revoked_row_is_not_touched(self, db: Session, fake_garmin: FakeGarmin) -> None:
        first = connected(ConnectionStatus.REVOKED)

        result = push(db, {"userPermissionsChange": [{"userId": GARMIN_USER, "permissions": []}]})

        assert result["userPermissionsChange"]["updated"] == 0
        db.refresh(first)
        assert first.scope == "ACTIVITY_EXPORT HEALTH_EXPORT"
        assert fake_garmin.calls == []


class TestRejectedRefresh:
    """Any other outbound Garmin call that finds its refresh token rejected ends the row too."""

    def test_it_clears_the_tokens_on_garmin(self, db: Session, fake_garmin: FakeGarmin) -> None:
        fake_garmin.token = Reply(status=400)
        connection = connected()

        with pytest.raises(HTTPException):
            GarminStrategy().oauth.refresh_access_token(db, connection.user_id, connection.refresh_token)

        assert_ended(db, connection)

    def test_another_provider_keeps_its_tokens(self, db: Session) -> None:
        user = UserFactory()
        connection = UserConnectionFactory(user=user, provider="polar", status=ConnectionStatus.ACTIVE)
        connection_repo = GarminStrategy().connection_repo

        connection_repo.mark_as_revoked(db, connection)

        db.refresh(connection)
        assert connection.status == ConnectionStatus.REVOKED
        assert connection.refresh_token is not None
