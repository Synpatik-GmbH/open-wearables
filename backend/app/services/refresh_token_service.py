import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from logging import Logger, getLogger
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, text

from app.config import settings
from app.database import DbSession
from app.models import RefreshToken
from app.repositories.refresh_token_repository import refresh_token_repository
from app.schemas.auth import TokenResponse, TokenType
from app.services.sdk_token_service import create_sdk_user_token
from app.utils.security import create_access_token


class RefreshTokenService:
    """Service for managing refresh tokens."""

    def __init__(self, log: Logger, now: Callable[[], datetime] | None = None) -> None:
        self.logger = log
        self.repo = refresh_token_repository
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _generate_refresh_token_id() -> str:
        """Generate an opaque refresh token ID with rt- prefix."""
        return f"rt-{secrets.token_hex(16)}"

    @staticmethod
    def _unauthorized() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @staticmethod
    def _read_fresh(db_session: DbSession, token_id: str) -> RefreshToken | None:
        """Read a token row from the database, bypassing the session's identity map.

        A re-read under the lock must see what is committed now, not an object cached by the first read.
        """
        stmt = select(RefreshToken).where(RefreshToken.id == token_id).execution_options(populate_existing=True)
        return db_session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def _read_successor(db_session: DbSession, token_id: str) -> RefreshToken | None:
        """The row that replaced `token_id` by rotation, if any (fork spec D-03), read fresh."""
        stmt = (
            select(RefreshToken).where(RefreshToken.rotated_from == token_id).execution_options(populate_existing=True)
        )
        return db_session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def _lock_user_app(db_session: DbSession, user_id: UUID, app_id: str) -> None:
        """Serialise every SDK token transaction for one (user, app) (fork spec D-06).

        Transaction-scoped: PostgreSQL releases it at commit or rollback.
        """
        db_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{user_id}:{app_id}"},
        )

    @staticmethod
    def _sdk_token_response(user_id: UUID, app_id: str, refresh_token: str) -> TokenResponse:
        return TokenResponse(
            access_token=create_sdk_user_token(app_id=app_id, user_id=str(user_id)),
            token_type="bearer",
            refresh_token=refresh_token,
            expires_in=settings.access_token_expire_minutes * 60,
        )

    def create_sdk_refresh_token(
        self, db_session: DbSession, user_id: UUID, app_id: str, rotated_from: str | None = None
    ) -> str:
        """Create a refresh token for an SDK token.

        Args:
            db_session: Database session
            user_id: The OpenWearables User ID
            app_id: The application ID that created the token
            rotated_from: The token this one replaces, when created by rotation (fork spec D-03)

        Returns:
            The refresh token string (rt-{hex})
        """
        token_id = self._generate_refresh_token_id()
        token = RefreshToken(
            id=token_id,
            token_type=TokenType.SDK,
            user_id=user_id,
            app_id=app_id,
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
            rotated_from=rotated_from,
        )
        self.repo.create(db_session, token)
        self.logger.debug(f"Created SDK refresh token for user {user_id}, app {app_id}")
        return token_id

    def create_developer_refresh_token(self, db_session: DbSession, developer_id: UUID) -> str:
        """Create a refresh token for a developer token.

        Args:
            db_session: Database session
            developer_id: The developer ID

        Returns:
            The refresh token string (rt-{hex})
        """
        token_id = self._generate_refresh_token_id()
        token = RefreshToken(
            id=token_id,
            token_type=TokenType.DEVELOPER,
            user_id=None,
            app_id=None,
            developer_id=developer_id,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        self.repo.create(db_session, token)
        self.logger.debug(f"Created developer refresh token for developer {developer_id}")
        return token_id

    def refresh_token(self, db_session: DbSession, refresh_token_str: str) -> TokenResponse:
        """Exchange a refresh token for a new access token.

        SDK tokens follow the fork spec 2026-09-11 (INV-01): serialised per (user, app), linked
        rotation. Developer tokens keep upstream's strict rotation unchanged (D-04).

        Raises:
            HTTPException: 401 if the refresh token is invalid or revoked
        """
        first_read = self._read_fresh(db_session, refresh_token_str)
        if first_read is None:
            raise self._unauthorized()
        if first_read.token_type != TokenType.SDK:
            return self._refresh_non_sdk(db_session, refresh_token_str)
        return self._refresh_sdk(db_session, first_read)

    def _refresh_sdk(self, db_session: DbSession, first_read: RefreshToken) -> TokenResponse:
        """SDK branch of INV-01. Primitives are captured before any commit (attributes expire on commit)."""
        token_id = first_read.id
        user_id = first_read.user_id
        app_id = first_read.app_id
        self._lock_user_app(db_session, user_id, app_id)  # ty:ignore[invalid-argument-type]
        token = self._read_fresh(db_session, token_id)
        if token is None:
            db_session.commit()  # nothing written; ends the transaction, releasing the lock
            raise self._unauthorized()
        if token.revoked_at is None:
            token.revoked_at = self._now()
            # create_sdk_refresh_token commits once: the revoke above and the successor insert together
            successor_id = self.create_sdk_refresh_token(
                db_session,
                user_id=user_id,  # ty:ignore[invalid-argument-type]
                app_id=app_id,  # ty:ignore[invalid-argument-type]
                rotated_from=token_id,
            )
            return self._sdk_token_response(user_id, app_id, successor_id)  # ty:ignore[invalid-argument-type]
        # token was revoked. Only a rotation leaves a successor naming it (D-03).
        successor = self._read_successor(db_session, token_id)
        if successor is None:
            reason = "revoked"
        elif successor.revoked_at is not None:
            reason = "rotated_successor_used"
        elif self._now() - token.revoked_at > timedelta(seconds=settings.sdk_refresh_grace_seconds):
            reason = "rotated_past_grace"
        else:
            successor_id = successor.id
            db_session.commit()  # grace writes nothing (INV-04); ends the transaction, releasing the lock
            return self._sdk_token_response(user_id, app_id, successor_id)  # ty:ignore[invalid-argument-type]
        db_session.commit()
        self.logger.debug(f"SDK refresh rejected: {reason}")
        raise self._unauthorized()

    def _refresh_non_sdk(self, db_session: DbSession, refresh_token_str: str) -> TokenResponse:
        """Upstream behaviour for developer tokens, unchanged (fork spec D-04)."""
        token = self.repo.get_valid_token(db_session, refresh_token_str)
        if not token:
            raise self._unauthorized()

        # Revoke the old refresh token (rotation)
        self.repo.revoke_token(db_session, token)

        if token.token_type == TokenType.DEVELOPER:
            access_token = create_access_token(subject=str(token.developer_id))
            new_refresh_token = self.create_developer_refresh_token(
                db_session,
                developer_id=token.developer_id,  # ty:ignore[invalid-argument-type]
            )
            self.logger.debug(f"Refreshed developer token for developer {token.developer_id} (rotated)")
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown token type: {token.token_type}",
            )

        return TokenResponse(
            access_token=access_token,
            token_type="bearer",
            refresh_token=new_refresh_token,
            expires_in=settings.access_token_expire_minutes * 60,
        )

    def revoke_token(self, db_session: DbSession, refresh_token_str: str) -> bool:
        """Revoke a refresh token.

        Args:
            db_session: Database session
            refresh_token_str: The refresh token string

        Returns:
            True if the token was revoked, False if not found

        Raises:
            HTTPException: If the refresh token is not found
        """
        token = self.repo.get_valid_token(db_session, refresh_token_str)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Refresh token not found",
            )

        self.repo.revoke_token(db_session, token)
        self.logger.debug(f"Revoked refresh token {refresh_token_str[:10]}...")
        return True


refresh_token_service = RefreshTokenService(log=getLogger(__name__))
