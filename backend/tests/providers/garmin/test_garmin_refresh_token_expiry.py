"""Every write of a Garmin refresh token also writes its expiry; every clear clears it (design D-15)."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import UserConnection
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.model_crud.credentials import OAuthTokenResponse
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from tests.factories import UserConnectionFactory, UserFactory

MIGRATION = Path(__file__).parents[3] / (
    "migrations/versions/2026_10_30_1600-c4f8a2d17e63_garmin_refresh_token_expiry.py"
)
LIFETIME = 7775998
repository = UserConnectionRepository(UserConnection)


def oauth() -> BaseOAuthTemplate:
    return GarminStrategy().oauth


def answer(**extra: object) -> OAuthTokenResponse:
    return OAuthTokenResponse(
        access_token="access", token_type="bearer", expires_in=86400, refresh_token="refresh", **extra
    )


def about_ninety_days_from_now(value: datetime | None) -> bool:
    return value is not None and abs(value - (datetime.now(timezone.utc) + timedelta(seconds=LIFETIME))) < timedelta(
        minutes=1
    )


class TestTheLifetime:
    @pytest.mark.parametrize(
        ("extra", "lifetime"),
        [
            pytest.param({"refresh_token_expires_in": LIFETIME}, LIFETIME, id="seconds"),
            pytest.param({}, None, id="absent"),
            pytest.param({"refresh_token_expires_in": 0}, None, id="zero"),
            pytest.param({"refresh_token_expires_in": -1}, None, id="negative"),
            pytest.param({"refresh_token_expires_in": "7775998"}, None, id="text"),
            pytest.param({"refresh_token_expires_in": True}, None, id="boolean"),
        ],
    )
    def test_only_a_positive_integer_is_a_lifetime(self, extra: dict, lifetime: int | None) -> None:
        assert answer(**extra).refresh_token_lifetime == lifetime


class TestEveryWrite:
    def test_a_first_connect_stores_it(self, db: Session) -> None:
        user = UserFactory()

        connection, _, _ = oauth()._store_connection(db, user.id, answer(refresh_token_expires_in=LIFETIME), {})

        assert about_ninety_days_from_now(connection.refresh_token_expires_at)

    def test_a_reconnect_stores_it(self, db: Session) -> None:
        row = UserConnectionFactory(user=UserFactory(), provider="garmin", refresh_token_expires_at=None)

        oauth()._store_connection(db, row.user_id, answer(refresh_token_expires_in=LIFETIME), {})

        db.refresh(row)
        assert about_ninety_days_from_now(row.refresh_token_expires_at)

    def test_a_reconnect_with_no_lifetime_drops_the_old_tokens_expiry(self, db: Session) -> None:
        old = datetime(2026, 12, 1, tzinfo=timezone.utc)
        row = UserConnectionFactory(user=UserFactory(), provider="garmin", refresh_token_expires_at=old)

        oauth()._store_connection(db, row.user_id, answer(), {})

        db.refresh(row)
        assert row.refresh_token_expires_at is None

    def test_a_generic_refresh_stores_it(self, db: Session) -> None:
        row = UserConnectionFactory(user=UserFactory(), provider="garmin", refresh_token_expires_at=None)
        body = answer(refresh_token_expires_in=LIFETIME).model_dump()
        response = httpx.Response(200, json=body, request=httpx.Request("POST", "https://token.example"))

        with patch("httpx.post", return_value=response):
            oauth().refresh_access_token(db, row.user_id, row.refresh_token)

        db.refresh(row)
        assert about_ninety_days_from_now(row.refresh_token_expires_at)


class TestEveryClear:
    def test_a_disconnect_clears_it(self, db: Session) -> None:
        row = UserConnectionFactory(
            user=UserFactory(), provider="garmin", refresh_token_expires_at=datetime(2026, 12, 1, tzinfo=timezone.utc)
        )

        repository.disconnect(db, row.user_id, "garmin")

        db.refresh(row)
        assert row.refresh_token_expires_at is None

    def test_mark_as_revoked_clears_it_for_garmin(self, db: Session) -> None:
        row = UserConnectionFactory(
            user=UserFactory(), provider="garmin", refresh_token_expires_at=datetime(2026, 12, 1, tzinfo=timezone.utc)
        )

        repository.mark_as_revoked(db, row)

        assert row.refresh_token_expires_at is None


class TestTheConditionalWrite:
    def write(self, db: Session, row_id: object, presented: str) -> bool:
        return repository.store_refreshed_tokens(
            db,
            row_id,
            presented_refresh_token=presented,
            access_token="new-access",
            refresh_token="new-refresh",
            expires_in=86400,
            refresh_token_expires_in=LIFETIME,
        )

    def test_it_writes_every_field_of_the_row_it_names(self, db: Session) -> None:
        row, other = (UserConnectionFactory(user=UserFactory(), refresh_token="same") for _ in range(2))
        other_tokens = other.access_token

        assert self.write(db, row.id, "same") is True

        db.refresh(row)
        db.refresh(other)
        assert (row.access_token, row.refresh_token) == ("new-access", "new-refresh")
        assert about_ninety_days_from_now(row.refresh_token_expires_at)
        assert (other.access_token, other.refresh_token) == (other_tokens, "same")

    def test_it_moves_updated_at_not_the_status(self, db: Session) -> None:
        then = datetime(2026, 1, 1, tzinfo=timezone.utc)
        row = UserConnectionFactory(user=UserFactory(), status=ConnectionStatus.EXPIRED, updated_at=then)

        self.write(db, row.id, row.refresh_token)

        db.refresh(row)
        assert row.status == ConnectionStatus.EXPIRED
        assert row.updated_at > then

    def test_a_revoked_row_is_not_written(self, db: Session) -> None:
        row = UserConnectionFactory(user=UserFactory(), status=ConnectionStatus.REVOKED, refresh_token="same")

        assert self.write(db, row.id, "same") is False

        db.refresh(row)
        assert row.refresh_token == "same"

    def test_a_row_holding_another_token_is_not_written(self, db: Session) -> None:
        row = UserConnectionFactory(user=UserFactory(), refresh_token="newer")

        assert self.write(db, row.id, "older") is False

        db.refresh(row)
        assert row.refresh_token == "newer"

    def test_a_missing_row_is_not_written(self, db: Session) -> None:
        assert self.write(db, uuid4(), "same") is False


class TestTheMigration:
    def module(self) -> object:
        spec = importlib.util.spec_from_file_location("garmin_refresh_token_expiry", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_row_from_before_it_keeps_its_tokens_with_an_unknown_expiry(self, db: Session) -> None:
        """Seeded at the previous shape: the column is dropped first, then the migration adds it."""
        row = UserConnectionFactory(user=UserFactory(), provider="garmin", refresh_token="held")
        row_id, access, refresh = row.id, row.access_token, row.refresh_token
        db.flush()
        with Operations.context(MigrationContext.configure(db.connection())):
            self.module().downgrade()
            assert "refresh_token_expires_at" not in {
                column["name"] for column in inspect(db.connection()).get_columns("user_connection")
            }
            self.module().upgrade()
        db.expire_all()

        row = db.get(UserConnection, row_id)
        assert (row.access_token, row.refresh_token) == (access, refresh)
        assert row.refresh_token_expires_at is None
