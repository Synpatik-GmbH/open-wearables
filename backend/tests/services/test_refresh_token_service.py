"""
Unit tests for refresh token service.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import RefreshToken
from app.schemas.auth import TokenType
from app.services.refresh_token_service import refresh_token_service
from tests.factories import DeveloperFactory, UserFactory


class TestCreateSDKRefreshToken:
    """Tests for create_sdk_refresh_token."""

    def test_create_sdk_refresh_token_format(self, db: Session) -> None:
        """SDK refresh token should have rt- prefix and be 35 characters total."""
        # Arrange
        user = UserFactory()
        app_id = "test_app_123"

        # Act
        token = refresh_token_service.create_sdk_refresh_token(db, user.id, app_id)

        # Assert
        assert token.startswith("rt-")
        assert len(token) == 35  # "rt-" (3) + 32 hex chars

    def test_create_sdk_refresh_token_stored_in_db(self, db: Session) -> None:
        """SDK refresh token should be stored in database with correct metadata."""
        # Arrange
        user = UserFactory()
        app_id = "test_app_123"

        # Act
        token = refresh_token_service.create_sdk_refresh_token(db, user.id, app_id)

        # Assert
        db_token = db.query(RefreshToken).filter(RefreshToken.id == token).first()
        assert db_token is not None
        assert db_token.token_type == TokenType.SDK
        assert db_token.user_id == user.id
        assert db_token.app_id == app_id
        assert db_token.developer_id is None
        assert db_token.revoked_at is None


class TestCreateDeveloperRefreshToken:
    """Tests for create_developer_refresh_token."""

    def test_create_developer_refresh_token_format(self, db: Session) -> None:
        """Developer refresh token should have rt- prefix and be 35 characters total."""
        # Arrange
        developer = DeveloperFactory()

        # Act
        token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        # Assert
        assert token.startswith("rt-")
        assert len(token) == 35  # "rt-" (3) + 32 hex chars

    def test_create_developer_refresh_token_stored_in_db(self, db: Session) -> None:
        """Developer refresh token should be stored in database with correct metadata."""
        # Arrange
        developer = DeveloperFactory()

        # Act
        token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        # Assert
        db_token = db.query(RefreshToken).filter(RefreshToken.id == token).first()
        assert db_token is not None
        assert db_token.token_type == TokenType.DEVELOPER
        assert db_token.developer_id == developer.id
        assert db_token.user_id is None
        assert db_token.app_id is None
        assert db_token.revoked_at is None


class TestRefreshToken:
    """Tests for refresh_token method."""

    def test_refresh_sdk_token_success(self, db: Session) -> None:
        """Refreshing SDK token should return new access token and rotated refresh token."""
        # Arrange
        user = UserFactory()
        app_id = "test_app_123"
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, app_id)

        # Act
        result = refresh_token_service.refresh_token(db, refresh_token)

        # Assert
        assert result.access_token is not None
        assert result.token_type == "bearer"
        # Refresh token should be rotated
        assert result.refresh_token != refresh_token
        assert result.refresh_token.startswith("rt-")

    def test_refresh_developer_token_success(self, db: Session) -> None:
        """Refreshing developer token should return new access token and rotated refresh token."""
        # Arrange
        developer = DeveloperFactory()
        refresh_token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        # Act
        result = refresh_token_service.refresh_token(db, refresh_token)

        # Assert
        assert result.access_token is not None
        assert result.token_type == "bearer"
        # Refresh token should be rotated
        assert result.refresh_token != refresh_token
        assert result.refresh_token.startswith("rt-")

    def test_refresh_invalid_token_raises_401(self, db: Session) -> None:
        """Refreshing invalid token should raise 401."""
        # Arrange
        invalid_token = "rt-invalidtoken12345678901234567890"

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, invalid_token)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid or revoked refresh token"

    def test_refresh_revoked_token_raises_401(self, db: Session) -> None:
        """Refreshing revoked token should raise 401."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        refresh_token_service.revoke_token(db, refresh_token)

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, refresh_token)

        assert exc_info.value.status_code == 401

    def test_refresh_revokes_old_token(self, db: Session) -> None:
        """Refreshing token should revoke the old token (rotation)."""
        # Arrange
        user = UserFactory()
        old_refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")

        # Verify not revoked initially
        db_token = db.query(RefreshToken).filter(RefreshToken.id == old_refresh_token).first()
        assert db_token is not None
        assert db_token.revoked_at is None

        # Act
        result = refresh_token_service.refresh_token(db, old_refresh_token)

        # Assert - old token is revoked
        db.refresh(db_token)
        assert db_token.revoked_at is not None

        # Assert - new token exists and is not revoked
        new_db_token = db.query(RefreshToken).filter(RefreshToken.id == result.refresh_token).first()
        assert new_db_token is not None
        assert new_db_token.revoked_at is None


class TestRevokeToken:
    """Tests for revoke_token method."""

    def test_revoke_token_success(self, db: Session) -> None:
        """Revoking token should set revoked_at timestamp."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")

        # Act
        result = refresh_token_service.revoke_token(db, refresh_token)

        # Assert
        assert result is True
        db_token = db.query(RefreshToken).filter(RefreshToken.id == refresh_token).first()
        assert db_token is not None
        assert db_token.revoked_at is not None

    def test_revoke_nonexistent_token_raises_404(self, db: Session) -> None:
        """Revoking non-existent token should raise 404."""
        # Arrange
        invalid_token = "rt-nonexistent123456789012345678"

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, invalid_token)

        assert exc_info.value.status_code == 404

    def test_revoke_already_revoked_token_raises_404(self, db: Session) -> None:
        """Revoking already revoked token should raise 404."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        refresh_token_service.revoke_token(db, refresh_token)

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, refresh_token)

        assert exc_info.value.status_code == 404


class TestSdkRotationIsLinkedAndSerialised:
    """INV-01 live branch and D-06 (fork spec 2026-09-11)."""

    def test_rotation_links_the_successor_to_the_presented_token(self, db: Session) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")

        result = refresh_token_service.refresh_token(db, old)

        successor = db.execute(select(RefreshToken).where(RefreshToken.id == result.refresh_token)).scalar_one()
        assert successor.rotated_from == old

    def test_sdk_refresh_takes_the_user_app_lock(self, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        calls: list[tuple[object, str]] = []
        real_lock = refresh_token_service._lock_user_app

        def spy(session: Session, user_id: object, app_id: str) -> None:
            calls.append((user_id, app_id))
            real_lock(session, user_id, app_id)  # ty: ignore[invalid-argument-type]

        monkeypatch.setattr(refresh_token_service, "_lock_user_app", spy)

        refresh_token_service.refresh_token(db, old)

        assert calls == [(user.id, "test_app")]

    def test_row_deleted_between_first_read_and_lock_is_rejected(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        real_lock = refresh_token_service._lock_user_app

        def lock_then_delete(session: Session, user_id: object, app_id: str) -> None:
            real_lock(session, user_id, app_id)  # ty: ignore[invalid-argument-type]
            session.execute(delete(RefreshToken).where(RefreshToken.id == old))

        monkeypatch.setattr(refresh_token_service, "_lock_user_app", lock_then_delete)

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, old)

        assert exc_info.value.status_code == 401


class TestDeveloperRefreshIsUnchanged:
    """D-04: developer tokens take today's code — strict rotation, no lock."""

    def test_old_developer_token_is_rejected_right_after_rotation(self, db: Session) -> None:
        developer = DeveloperFactory()
        old = refresh_token_service.create_developer_refresh_token(db, developer.id)
        refresh_token_service.refresh_token(db, old)

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, old)

        assert exc_info.value.status_code == 401

    def test_developer_refresh_takes_no_lock(self, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        developer = DeveloperFactory()
        old = refresh_token_service.create_developer_refresh_token(db, developer.id)

        def fail(*_args: object) -> None:
            raise AssertionError("developer refresh must not take the SDK lock")

        monkeypatch.setattr(refresh_token_service, "_lock_user_app", fail)

        result = refresh_token_service.refresh_token(db, old)

        assert result.refresh_token != old


class TestSdkRefreshGrace:
    """D-02, D-03, D-05, INV-01, INV-04 (fork spec 2026-09-11)."""

    def test_superseded_token_with_unrevoked_successor_gets_the_same_successor(self, db: Session) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        first = refresh_token_service.refresh_token(db, old)

        again = refresh_token_service.refresh_token(db, old)

        assert again.refresh_token == first.refresh_token

    def test_grace_is_idempotent_and_writes_no_rows(self, db: Session) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        first = refresh_token_service.refresh_token(db, old)
        rows_before = db.query(RefreshToken).filter(RefreshToken.user_id == user.id).count()

        a = refresh_token_service.refresh_token(db, old)
        b = refresh_token_service.refresh_token(db, old)

        assert a.refresh_token == b.refresh_token == first.refresh_token
        assert db.query(RefreshToken).filter(RefreshToken.user_id == user.id).count() == rows_before

    def test_superseded_token_is_rejected_once_its_successor_was_rotated(self, db: Session) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        successor = refresh_token_service.refresh_token(db, old).refresh_token
        refresh_token_service.refresh_token(db, successor)  # the client applied the successor

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, old)

        assert exc_info.value.status_code == 401

    def test_superseded_token_is_rejected_once_its_successor_was_revoked(self, db: Session) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        successor = refresh_token_service.refresh_token(db, old).refresh_token
        token = db.execute(select(RefreshToken).where(RefreshToken.id == successor)).scalar_one()
        refresh_token_service.repo.revoke_token(db, token)

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, old)

        assert exc_info.value.status_code == 401

    def test_token_revoked_while_live_gets_no_grace(self, db: Session) -> None:
        user = UserFactory()
        token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        refresh_token_service.revoke_token(db, token)

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, token)

        assert exc_info.value.status_code == 401

    def test_grace_boundary_is_inclusive(self, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        user = UserFactory()
        rotated_at = datetime(2026, 9, 11, 15, 10, 4, tzinfo=timezone.utc)
        grace = timedelta(seconds=settings.sdk_refresh_grace_seconds)
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        monkeypatch.setattr(refresh_token_service, "_now", lambda: rotated_at)
        successor = refresh_token_service.refresh_token(db, old).refresh_token

        monkeypatch.setattr(refresh_token_service, "_now", lambda: rotated_at + grace)
        assert refresh_token_service.refresh_token(db, old).refresh_token == successor

        monkeypatch.setattr(refresh_token_service, "_now", lambda: rotated_at + grace + timedelta(seconds=1))
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, old)
        assert exc_info.value.status_code == 401
