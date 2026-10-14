"""2.96.6.1 (spec cause 4, AC6, L1): a Polar connection is not announced, pulled or reported
as connected until Polar accepted the user's registration, and a failed registration is retried."""

from collections.abc import Iterator
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pytest
from sqlalchemy.orm import Session

from app.models import PolarRegistration, UserConnection
from app.schemas.auth import ConnectionStatus
from app.schemas.model_crud.credentials import OAuthState, OAuthTokenResponse
from app.services.providers.polar.strategy import PolarStrategy
from tests.factories import UserConnectionFactory, UserFactory


def _answer(status: int) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    return response


@pytest.fixture
def announced() -> Iterator[list[dict]]:
    calls: list[dict] = []
    with patch(
        "app.services.providers.templates.base_oauth.on_connection_created",
        side_effect=lambda **kw: calls.append(kw),
    ):
        yield calls


def _callback(db: Session, user_id: UUID, *, x_user_id: int | None = 4711) -> UserConnection:
    oauth = PolarStrategy().oauth
    token = OAuthTokenResponse(
        access_token="access", refresh_token="refresh", expires_in=3600, token_type="Bearer", x_user_id=x_user_id
    )
    user_info = oauth._get_provider_user_info(token, str(user_id))
    state = OAuthState(user_id=user_id, provider="polar")
    oauth._save_connection(db, user_id, token, user_info, state)
    return db.query(UserConnection).filter(UserConnection.user_id == user_id).one()


def _registration(db: Session, connection_id: UUID) -> PolarRegistration | None:
    db.expire_all()
    return db.get(PolarRegistration, connection_id)


class TestAtConnect:
    @pytest.mark.parametrize("status", [200, 201, 409])
    def test_an_accepted_registration_is_recorded_and_announced(
        self, status: int, db: Session, announced: list[dict]
    ) -> None:
        user = UserFactory()
        with patch("app.services.providers.polar.oauth.httpx.post", return_value=_answer(status)):
            connection = _callback(db, user.id)

        row = _registration(db, connection.id)
        assert row.registered is True
        assert row.last_error_code is None
        assert [a["connection_id"] for a in announced] == [connection.id]

    @pytest.mark.parametrize(
        ("answer", "code"),
        [
            (_answer(500), "HTTPStatusError_500"),
            (_answer(403), "HTTPStatusError_403"),
            (httpx.ConnectError("refused, with text that must not be kept"), "ConnectError"),
        ],
    )
    def test_a_failed_registration_is_pending_with_a_code_and_not_announced(
        self, answer: object, code: str, db: Session, announced: list[dict]
    ) -> None:
        user = UserFactory()
        kwargs = {"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}
        with patch("app.services.providers.polar.oauth.httpx.post", **kwargs):
            connection = _callback(db, user.id)

        row = _registration(db, connection.id)
        assert row.registered is False
        assert row.last_error_code == code
        assert announced == []

    def test_no_polar_user_id_is_pending_without_calling_polar(self, db: Session, announced: list[dict]) -> None:
        user = UserFactory()
        with patch("app.services.providers.polar.oauth.httpx.post") as post:
            connection = _callback(db, user.id, x_user_id=None)

        post.assert_not_called()
        assert _registration(db, connection.id).last_error_code == "polar_user_id_missing"
        assert announced == []


class TestAtReconnect:
    def test_a_reconnect_that_turns_a_pending_registration_into_success_is_announced(
        self, db: Session, announced: list[dict]
    ) -> None:
        # The connection stays ACTIVE across the reconnect, so the base save does not announce it;
        # the pending registration it had never was, and is now.
        user = UserFactory()
        with patch("app.services.providers.polar.oauth.httpx.post", return_value=_answer(503)):
            connection = _callback(db, user.id)
        assert announced == []

        with patch("app.services.providers.polar.oauth.httpx.post", return_value=_answer(200)):
            _callback(db, user.id)

        assert _registration(db, connection.id).registered is True
        assert [a["connection_id"] for a in announced] == [connection.id]


class TestAtEveryPull:
    def _connection(self, db: Session, *, registered: bool | None) -> UserConnection:
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user, provider="polar", provider_user_id="4711", status=ConnectionStatus.ACTIVE
        )
        if registered is not None:
            db.add(
                PolarRegistration(
                    user_connection_id=connection.id,
                    registered=registered,
                    attempts=1,
                    last_error_code=None if registered else "HTTPStatusError_500",
                    updated_at=datetime.now(timezone.utc),
                )
            )
        db.commit()
        return connection

    def test_a_pending_registration_that_still_fails_refuses_the_pull(self, db: Session, announced: list[dict]) -> None:
        connection = self._connection(db, registered=False)

        with patch("app.services.providers.polar.oauth.httpx.post", return_value=_answer(503)):
            refused = PolarStrategy().prepare_pull(db, connection)

        row = _registration(db, connection.id)
        assert refused == "HTTPStatusError_503"
        assert row.registered is False
        assert row.attempts == 2
        assert announced == []

    def test_a_pending_registration_that_now_succeeds_is_announced_and_the_pull_goes_ahead(
        self, db: Session, announced: list[dict]
    ) -> None:
        connection = self._connection(db, registered=False)

        with patch("app.services.providers.polar.oauth.httpx.post", return_value=_answer(200)):
            refused = PolarStrategy().prepare_pull(db, connection)

        assert refused is None
        assert _registration(db, connection.id).registered is True
        assert [a["connection_id"] for a in announced] == [connection.id]

    def test_a_registered_connection_does_not_call_polar(self, db: Session, announced: list[dict]) -> None:
        connection = self._connection(db, registered=True)

        with patch("app.services.providers.polar.oauth.httpx.post") as post:
            refused = PolarStrategy().prepare_pull(db, connection)

        assert refused is None
        post.assert_not_called()

    def test_a_connection_made_before_the_fix_is_attempted_once_and_not_announced_again(
        self, db: Session, announced: list[dict]
    ) -> None:
        connection = self._connection(db, registered=None)

        with patch("app.services.providers.polar.oauth.httpx.post", return_value=_answer(409)):
            refused = PolarStrategy().prepare_pull(db, connection)

        assert refused is None
        assert _registration(db, connection.id).registered is True
        assert announced == []
