"""FORK migration: revoked Garmin rows from before this build lose their tokens (2.71.3.3, D-05)."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from app.models import UserConnection
from app.schemas.auth import ConnectionStatus
from tests.factories import UserConnectionFactory, UserFactory

PATH = (
    Path(__file__).parents[3] / "migrations/versions/2026_10_30_0900-b7e1c4a90d52_revoked_garmin_rows_hold_no_token.py"
)
UPDATED_AT = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def migration() -> object:
    spec = importlib.util.spec_from_file_location("revoked_garmin_rows_hold_no_token", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed(provider: str, status: ConnectionStatus, **tokens: object) -> UserConnection:
    return UserConnectionFactory(user=UserFactory(), provider=provider, status=status, updated_at=UPDATED_AT, **tokens)


def run(db: Session) -> None:
    db.flush()
    with Operations.context(MigrationContext.configure(db.connection())):
        migration().upgrade()
    db.expire_all()


def tokens(connection: UserConnection) -> tuple[object, object, object]:
    return connection.access_token, connection.refresh_token, connection.token_expires_at


def test_a_revoked_garmin_row_that_holds_tokens_loses_all_three(db: Session) -> None:
    connection = seed("garmin", ConnectionStatus.REVOKED)

    run(db)

    assert tokens(connection) == (None, None, None)


def test_a_row_holding_only_one_of_them_loses_it(db: Session) -> None:
    only_refresh = seed("garmin", ConnectionStatus.REVOKED, access_token=None, token_expires_at=None)
    only_expiry = seed("garmin", ConnectionStatus.REVOKED, access_token=None, refresh_token=None)

    run(db)

    assert tokens(only_refresh) == (None, None, None)
    assert tokens(only_expiry) == (None, None, None)


def test_a_connection_still_live_keeps_its_tokens(db: Session) -> None:
    active = seed("garmin", ConnectionStatus.ACTIVE)
    expired = seed("garmin", ConnectionStatus.EXPIRED)

    run(db)

    assert None not in tokens(active)
    assert None not in tokens(expired)


def test_another_providers_revoked_row_keeps_its_tokens(db: Session) -> None:
    polar = seed("polar", ConnectionStatus.REVOKED)

    run(db)

    assert None not in tokens(polar)


def test_it_does_not_move_when_the_connection_ended(db: Session) -> None:
    """updated_at is what connection.revoked reported as the end; a cleanup is not an end."""
    connection = seed("garmin", ConnectionStatus.REVOKED)

    run(db)

    assert connection.updated_at == UPDATED_AT
