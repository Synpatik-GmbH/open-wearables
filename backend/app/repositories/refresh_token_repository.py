from datetime import datetime, timezone
from typing import cast
from uuid import UUID

from sqlalchemy import CursorResult, select, text, update

from app.database import DbSession
from app.models import RefreshToken
from app.schemas.auth import TokenType


class RefreshTokenRepository:
    """Repository for refresh token database operations."""

    def __init__(self) -> None:
        self.model = RefreshToken

    def create(self, db_session: DbSession, token: RefreshToken) -> RefreshToken:
        """Create a new refresh token."""
        db_session.add(token)
        db_session.commit()
        db_session.refresh(token)
        return token

    def get_valid_token(self, db_session: DbSession, token_id: str) -> RefreshToken | None:
        """Get a refresh token if it exists and is not revoked."""
        stmt = select(self.model).where(self.model.id == token_id, self.model.revoked_at.is_(None))
        return db_session.execute(stmt).scalar_one_or_none()

    def get_by_user_id(self, db_session: DbSession, user_id: UUID) -> list[RefreshToken]:
        """Get all refresh tokens for a user."""
        stmt = select(self.model).where(self.model.user_id == user_id, self.model.revoked_at.is_(None))
        return list(db_session.execute(stmt).scalars().all())

    def get_by_developer_id(self, db_session: DbSession, developer_id: UUID) -> list[RefreshToken]:
        """Get all refresh tokens for a developer."""
        stmt = select(self.model).where(self.model.developer_id == developer_id, self.model.revoked_at.is_(None))
        return list(db_session.execute(stmt).scalars().all())

    def revoke_token(self, db_session: DbSession, token: RefreshToken) -> RefreshToken:
        """Revoke a single refresh token."""
        token.revoked_at = datetime.now(timezone.utc)
        db_session.commit()
        db_session.refresh(token)
        return token

    def revoke_all_for_user(self, db_session: DbSession, user_id: UUID) -> int:
        """Revoke all refresh tokens for a user. Returns count of revoked tokens."""
        now = datetime.now(timezone.utc)
        stmt = (
            update(self.model)
            .where(self.model.user_id == user_id, self.model.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        result = cast(CursorResult[tuple[()]], db_session.execute(stmt))
        db_session.commit()
        return result.rowcount or 0

    def revoke_all_for_developer(self, db_session: DbSession, developer_id: UUID) -> int:
        """Revoke all refresh tokens for a developer. Returns count of revoked tokens."""
        now = datetime.now(timezone.utc)
        stmt = (
            update(self.model)
            .where(self.model.developer_id == developer_id, self.model.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        result = cast(CursorResult[tuple[()]], db_session.execute(stmt))
        db_session.commit()
        return result.rowcount or 0

    def update_last_used(self, db_session: DbSession, token: RefreshToken) -> RefreshToken:
        """Update the last_used_at timestamp of a token."""
        token.last_used_at = datetime.now(timezone.utc)
        db_session.commit()
        db_session.refresh(token)
        return token

    # ---- Fork additions (spec 2026-09-11 §5.2): the SDK paths' reads, lock and writes.
    # None of these commit. SDK refresh, revoke and mint each run as ONE transaction under
    # `lock_user_app`, and `pg_advisory_xact_lock` is held only for that transaction — a commit in
    # any of these would release the lock mid-operation. The caller owns the boundary: each SDK path
    # is closed by ONE commit — from `create`/`revoke_token` on a path that writes, and from the
    # service on a path that writes nothing. The commit-per-call helpers above are unchanged.

    def get_by_id(self, db_session: DbSession, token_id: str) -> RefreshToken | None:
        """Read a token row whatever its state, bypassing the session's identity map.

        `get_valid_token` hides revoked rows; the SDK paths must see one to judge grace (D-02).
        A re-read under the lock must see what is committed now, not an object cached by the
        first read, hence `populate_existing` (D-06).
        """
        stmt = select(self.model).where(self.model.id == token_id).execution_options(populate_existing=True)
        return db_session.execute(stmt).scalar_one_or_none()

    def get_successor(self, db_session: DbSession, token_id: str) -> RefreshToken | None:
        """The row that replaced `token_id` by rotation, if any (fork spec D-03), read fresh.

        Only rotation writes `rotated_from`, which is what distinguishes "superseded" from
        "revoked on purpose" (INV-02).
        """
        stmt = select(self.model).where(self.model.rotated_from == token_id).execution_options(populate_existing=True)
        return db_session.execute(stmt).scalar_one_or_none()

    def lock_user_app(self, db_session: DbSession, user_id: UUID, app_id: str) -> None:
        """Serialise every SDK token transaction for one `(user, app)` (fork spec D-06).

        Transaction-scoped: PostgreSQL releases it at commit or rollback. Row locks could not do
        this — a mint's `UPDATE ... WHERE revoked_at IS NULL` cannot see a successor inserted
        after the statement began.
        """
        db_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{user_id}:{app_id}"},
        )

    def mark_revoked(self, token: RefreshToken, revoked_at: datetime) -> None:
        """Revoke `token` within the caller's open transaction, at the instant it supplies.

        The SDK rotation commits this revoke together with the successor insert (INV-01), so it
        must not commit on its own.
        """
        token.revoked_at = revoked_at

    def revoke_live_sdk_tokens(self, db_session: DbSession, user_id: UUID, app_id: str, revoked_at: datetime) -> int:
        """Revoke every unrevoked SDK token for one `(user, app)` (fork spec D-11). Returns the count.

        Scoped to `token_type = sdk`, so a developer token is never touched (D-04). Does not commit:
        the mint commits this together with its own insert.
        """
        stmt = (
            update(self.model)
            .where(
                self.model.token_type == TokenType.SDK,
                self.model.user_id == user_id,
                self.model.app_id == app_id,
                self.model.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        result = cast(CursorResult[tuple[()]], db_session.execute(stmt))
        return result.rowcount or 0


refresh_token_repository = RefreshTokenRepository()
