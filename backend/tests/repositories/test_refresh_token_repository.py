"""
Unit tests for refresh token repository.
"""

from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import RefreshToken
from app.repositories.refresh_token_repository import refresh_token_repository
from app.schemas.auth import TokenType
from tests.factories import DeveloperFactory, UserFactory


class TestRefreshTokenRepository:
    """Tests for RefreshTokenRepository."""

    def test_create_token(self, db: Session) -> None:
        """Creating a token should persist it to the database."""
        # Arrange
        user = UserFactory()
        token = RefreshToken(
            id="rt-test123456789012345678901234",
            token_type=TokenType.SDK,
            user_id=user.id,
            app_id="test_app",
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )

        # Act
        created = refresh_token_repository.create(db, token)

        # Assert
        assert created.id == "rt-test123456789012345678901234"
        assert created.token_type == TokenType.SDK
        assert created.user_id == user.id

    def test_get_valid_token(self, db: Session) -> None:
        """get_valid_token should return token if not revoked."""
        # Arrange
        user = UserFactory()
        token = RefreshToken(
            id="rt-validtoken123456789012345678",
            token_type=TokenType.SDK,
            user_id=user.id,
            app_id="test_app",
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        db.add(token)
        db.commit()

        # Act
        result = refresh_token_repository.get_valid_token(db, token.id)

        # Assert
        assert result is not None
        assert result.id == token.id

    def test_get_valid_token_returns_none_for_revoked(self, db: Session) -> None:
        """get_valid_token should return None if token is revoked."""
        # Arrange
        user = UserFactory()
        token = RefreshToken(
            id="rt-revokedtoken12345678901234567",
            token_type=TokenType.SDK,
            user_id=user.id,
            app_id="test_app",
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=datetime.now(timezone.utc),
        )
        db.add(token)
        db.commit()

        # Act
        result = refresh_token_repository.get_valid_token(db, token.id)

        # Assert
        assert result is None

    def test_get_by_user_id(self, db: Session) -> None:
        """get_by_user_id should return all non-revoked tokens for a user."""
        # Arrange
        user = UserFactory()
        token1 = RefreshToken(
            id="rt-user1token1234567890123456789",
            token_type=TokenType.SDK,
            user_id=user.id,
            app_id="app1",
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        token2 = RefreshToken(
            id="rt-user1token2345678901234567890",
            token_type=TokenType.SDK,
            user_id=user.id,
            app_id="app2",
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        db.add(token1)
        db.add(token2)
        db.commit()

        # Act
        result = refresh_token_repository.get_by_user_id(db, user.id)

        # Assert
        assert len(result) == 2

    def test_get_by_developer_id(self, db: Session) -> None:
        """get_by_developer_id should return all non-revoked tokens for a developer."""
        # Arrange
        developer = DeveloperFactory()
        token1 = RefreshToken(
            id="rt-dev1token12345678901234567890",
            token_type=TokenType.DEVELOPER,
            user_id=None,
            app_id=None,
            developer_id=developer.id,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        db.add(token1)
        db.commit()

        # Act
        result = refresh_token_repository.get_by_developer_id(db, developer.id)

        # Assert
        assert len(result) == 1
        assert result[0].developer_id == developer.id

    def test_revoke_token(self, db: Session) -> None:
        """revoke_token should set revoked_at timestamp."""
        # Arrange
        user = UserFactory()
        token = RefreshToken(
            id="rt-torevoke123456789012345678901",
            token_type=TokenType.SDK,
            user_id=user.id,
            app_id="test_app",
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        db.add(token)
        db.commit()

        # Act
        refresh_token_repository.revoke_token(db, token)

        # Assert
        db.refresh(token)
        assert token.revoked_at is not None

    def test_revoke_all_for_user(self, db: Session) -> None:
        """revoke_all_for_user should revoke all tokens for a user."""
        # Arrange
        user = UserFactory()
        token1 = RefreshToken(
            id="rt-revokeall123456789012345678901",
            token_type=TokenType.SDK,
            user_id=user.id,
            app_id="app1",
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        token2 = RefreshToken(
            id="rt-revokeall234567890123456789012",
            token_type=TokenType.SDK,
            user_id=user.id,
            app_id="app2",
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        db.add(token1)
        db.add(token2)
        db.commit()

        # Act
        count = refresh_token_repository.revoke_all_for_user(db, user.id)

        # Assert
        assert count == 2
        db.refresh(token1)
        db.refresh(token2)
        assert token1.revoked_at is not None
        assert token2.revoked_at is not None

    def test_revoke_all_for_developer(self, db: Session) -> None:
        """revoke_all_for_developer should revoke all tokens for a developer."""
        # Arrange
        developer = DeveloperFactory()
        token = RefreshToken(
            id="rt-revokedev123456789012345678901",
            token_type=TokenType.DEVELOPER,
            user_id=None,
            app_id=None,
            developer_id=developer.id,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        db.add(token)
        db.commit()

        # Act
        count = refresh_token_repository.revoke_all_for_developer(db, developer.id)

        # Assert
        assert count == 1
        db.refresh(token)
        assert token.revoked_at is not None

    def test_update_last_used(self, db: Session) -> None:
        """update_last_used should set last_used_at timestamp."""
        # Arrange
        user = UserFactory()
        token = RefreshToken(
            id="rt-updateused12345678901234567890",
            token_type=TokenType.SDK,
            user_id=user.id,
            app_id="test_app",
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        db.add(token)
        db.commit()
        assert token.last_used_at is None

        # Act
        refresh_token_repository.update_last_used(db, token)

        # Assert
        db.refresh(token)
        assert token.last_used_at is not None


class TestRotatedFromLink:
    """INV-02: one presented token can never have two successors."""

    def test_second_successor_for_same_token_is_rejected_by_the_database(self, db: Session) -> None:
        user = UserFactory()
        now = datetime.now(timezone.utc)
        predecessor = RefreshToken(
            id="rt-" + "0" * 32, token_type=TokenType.SDK, user_id=user.id, app_id="app", created_at=now
        )
        db.add(predecessor)
        db.flush()
        db.add(
            RefreshToken(
                id="rt-" + "1" * 32,
                token_type=TokenType.SDK,
                user_id=user.id,
                app_id="app",
                created_at=now,
                rotated_from=predecessor.id,
            )
        )
        db.flush()
        db.add(
            RefreshToken(
                id="rt-" + "2" * 32,
                token_type=TokenType.SDK,
                user_id=user.id,
                app_id="app",
                created_at=now,
                rotated_from=predecessor.id,
            )
        )

        with pytest.raises(IntegrityError):
            db.flush()


class TestForkSdkTransactionMethods:
    """§5.2 (fork spec 2026-09-11): the repository owns the SDK paths' reads, lock and writes.

    None of these commit. SDK refresh, revoke and mint each run as ONE transaction whose boundary
    the service owns, and `pg_advisory_xact_lock` is held only for that transaction — a commit in
    any of these would release the lock mid-operation. That contract is held by the two-connection
    tests in `tests/integrations/test_sdk_refresh_concurrency.py`: this fixture runs inside one
    connection and a savepoint, where a commit is not a real commit.
    """

    @staticmethod
    def _sdk_token(
        user_id: UUID,
        app_id: str,
        token_id: str,
        *,
        revoked_at: datetime | None = None,
        rotated_from: str | None = None,
    ) -> RefreshToken:
        return RefreshToken(
            id=token_id,
            token_type=TokenType.SDK,
            user_id=user_id,
            app_id=app_id,
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=revoked_at,
            rotated_from=rotated_from,
        )

    def test_get_by_id_returns_a_revoked_row(self, db: Session) -> None:
        """Unlike `get_valid_token`, the SDK paths must see a revoked row to judge grace (D-02)."""
        user = UserFactory()
        token = self._sdk_token(user.id, "calibra_app", "rt-" + "a" * 32, revoked_at=datetime.now(timezone.utc))
        db.add(token)
        db.commit()

        assert refresh_token_repository.get_valid_token(db, token.id) is None
        found = refresh_token_repository.get_by_id(db, token.id)
        assert found is not None
        assert found.id == token.id

    def test_get_by_id_bypasses_the_identity_map(self, db: Session) -> None:
        """The re-read under the lock must see what is committed now, not a cached object (D-06)."""
        user = UserFactory()
        token = self._sdk_token(user.id, "calibra_app", "rt-" + "b" * 32)
        db.add(token)
        db.commit()
        assert token.revoked_at is None

        # Change the row behind the ORM's back: raw SQL leaves the identity map untouched.
        db.execute(
            text("UPDATE refresh_token SET revoked_at = :ts WHERE id = :id"),
            {"ts": datetime.now(timezone.utc), "id": token.id},
        )

        found = refresh_token_repository.get_by_id(db, token.id)
        assert found is not None
        assert found.revoked_at is not None

    def test_get_successor_returns_the_row_that_names_the_token(self, db: Session) -> None:
        user = UserFactory()
        predecessor = self._sdk_token(user.id, "calibra_app", "rt-" + "c" * 32)
        db.add(predecessor)
        db.flush()
        successor = self._sdk_token(user.id, "calibra_app", "rt-" + "d" * 32, rotated_from=predecessor.id)
        db.add(successor)
        db.commit()

        found = refresh_token_repository.get_successor(db, predecessor.id)
        assert found is not None
        assert found.id == successor.id

    def test_get_successor_returns_none_when_no_row_names_the_token(self, db: Session) -> None:
        """A token revoked on purpose, or rotated before deploy, has no successor naming it (D-09)."""
        user = UserFactory()
        token = self._sdk_token(user.id, "calibra_app", "rt-" + "e" * 32, revoked_at=datetime.now(timezone.utc))
        db.add(token)
        db.commit()

        assert refresh_token_repository.get_successor(db, token.id) is None

    def test_lock_user_app_holds_one_advisory_lock_on_the_user_app_key(self, db: Session) -> None:
        """D-06's key is `hashtextextended(f"{user_id}:{app_id}", 0)`, taken in the one-argument form."""
        user = UserFactory()
        key = db.execute(text("SELECT hashtextextended(:k, 0)"), {"k": f"{user.id}:calibra_app"}).scalar_one()
        held_before = db.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
        ).scalar_one()

        refresh_token_repository.lock_user_app(db, user.id, "calibra_app")

        assert held_before == 0
        held = db.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                "AND classid::bigint = :classid AND objid::bigint = :objid AND objsubid = 1"
            ),
            {"classid": (key >> 32) & 0xFFFFFFFF, "objid": key & 0xFFFFFFFF},
        ).scalar_one()
        assert held == 1

    def test_revoke_live_sdk_tokens_is_scoped_to_the_user_app_and_sdk_type(self, db: Session) -> None:
        """D-11 revokes this phone's earlier chains for one `(user, app)` and nothing else."""
        user = UserFactory()
        other_user = UserFactory()
        developer = DeveloperFactory()
        already = datetime(2026, 1, 1, tzinfo=timezone.utc)
        target = self._sdk_token(user.id, "calibra_app", "rt-" + "f" * 32)
        other_app = self._sdk_token(user.id, "other_app", "rt-" + "0" * 32)
        other_users_token = self._sdk_token(other_user.id, "calibra_app", "rt-" + "1" * 32)
        already_revoked = self._sdk_token(user.id, "calibra_app", "rt-" + "2" * 32, revoked_at=already)
        developer_token = RefreshToken(
            id="rt-" + "3" * 32,
            token_type=TokenType.DEVELOPER,
            user_id=None,
            app_id=None,
            developer_id=developer.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add_all([target, other_app, other_users_token, already_revoked, developer_token])
        db.commit()
        moment = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)

        count = refresh_token_repository.revoke_live_sdk_tokens(db, user.id, "calibra_app", moment)

        assert count == 1
        for token in (target, other_app, other_users_token, already_revoked, developer_token):
            db.refresh(token)
        assert target.revoked_at == moment
        assert other_app.revoked_at is None
        assert other_users_token.revoked_at is None
        assert developer_token.revoked_at is None
        assert already_revoked.revoked_at == already  # not re-stamped

    def test_mark_revoked_uses_the_instant_it_is_given(self, db: Session) -> None:
        """The service passes its own clock so a test can pin the grace boundary (D-05)."""
        user = UserFactory()
        token = self._sdk_token(user.id, "calibra_app", "rt-" + "4" * 32)
        db.add(token)
        db.commit()
        moment = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)

        refresh_token_repository.mark_revoked(token, moment)
        db.flush()

        persisted = refresh_token_repository.get_by_id(db, token.id)
        assert persisted is not None
        assert persisted.revoked_at == moment
