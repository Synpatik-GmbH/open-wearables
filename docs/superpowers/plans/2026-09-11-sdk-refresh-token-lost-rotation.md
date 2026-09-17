# SDK Refresh-Token Lost-Rotation Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A phone that never received a rotation reply keeps syncing instead of being locked out; a reconnect revokes the user's earlier SDK tokens; every outcome is logged for the stuck-client query.

**Architecture:** `refresh_token_service.py` (OW fork) orchestrates only. Every query, the advisory lock and every write live in `refresh_token_repository.py`, per the spec's §5.2 and `backend/AGENTS.md:297` — the services layer issues no query and no write directly. The five new repository methods do **not** commit; each SDK path is one transaction closed by a single commit, issued by `repo.create`/`repo.revoke_token` on a path that writes and by the service on a path that writes nothing, because `pg_advisory_xact_lock` is held only for that transaction. Plus one nullable column. SDK refresh, revoke and mint each run in one transaction that first takes a per-`(user_id, app_id)` PostgreSQL advisory lock. A successor row names its predecessor in a unique `rotated_from` column; a superseded token presented while its successor is unrevoked (and within `sdk_refresh_grace_seconds`) gets the same successor back. Developer tokens keep today's code.

**Tech Stack:** Python 3.13, FastAPI 0.138, SQLAlchemy 2.0.51 (sync `Session`), PostgreSQL (16 in Azure, 18 in tests), Alembic 1.18, pydantic-settings 2.14, pytest + Testcontainers, uv, ruff, ty.

**Spec:** `docs/superpowers/specs/2026-09-11-sdk-refresh-token-lost-rotation-design.md` (IDs D-xx / INV-xx below refer to it). Consumer query: `docs/superpowers/specs/queries/2026-09-11-sdk-refresh-stuck-clients.kql`.

## Global Constraints

- Work only in the worktree `/private/tmp/claude-501/-Users-dragan-Desktop-AI-Agent-Calibra/3f480eb8-3f6f-48a0-978f-b798a6ae16b3/scratchpad/ow-refresh-grace` on branch `fix/sdk-refresh-token-grace`, based on `origin/release/0.6.2-syn` (= tag `0.6.2-syn.6`). Never `git checkout` in the shared clone `open-wearables/`. **Never push. Never open a PR.** Dragan decides both.
- Scope is SDK refresh tokens (`token_type == TokenType.SDK`). Developer tokens take today's code, byte-for-byte in behaviour (D-04): no lock, no link, no grace, no cascade.
- Advisory lock SQL, exactly: `SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))` with `key = f"{user_id}:{app_id}"` (D-06).
- Unchanged response bodies: refresh rejections are `401`, `detail="Invalid or revoked refresh token"`, header `WWW-Authenticate: Bearer`; revoke misses are `404`, `detail="Refresh token not found"`.
- Setting: `sdk_refresh_grace_seconds: int = 604800`, must be `> 0` (D-05, Q-01). Grace boundary is inclusive: `now − rotated_at == grace` is accepted.
- Log strings, exactly (D-08): actions `refresh_token_rotated`, `refresh_token_grace_reissued`, `refresh_token_rejected`; reasons `unknown`, `revoked`, `rotated_successor_used`, `rotated_past_grace`. Emitted with `log_structured(logger, "info", <action>, action=<action>, ...)`.
- Never read an ORM attribute after `db_session.commit()` inside the service: the sync session expires attributes on commit, and a lazy reload would run outside the lock. Capture primitives (ids, datetimes) before committing.
- Re-reads under the lock must bypass the identity map: `select(...).execution_options(populate_existing=True)`.
- TDD for every task: failing test first, watch it fail, minimal code, watch it pass. **Every guard gets a mutation drill** (remove the guard → the named test goes red → restore); write the drill result in the commit body.
- One task = one commit. Conventional title (`fix(auth): …`, `test(auth): …`, `docs(auth): …`). End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01GtCGV8A1bgi4FKQX7hkAQb
  ```
- Models: each task is implemented by an **Opus** subagent and reviewed by a **Fable** subagent; the final whole-fix review (Task 8) is **Fable**.
- Commands run from `backend/` with `export PATH="$HOME/.local/bin:$PATH"`. Focused tests: `uv run pytest <path> -v --no-cov`. Lint gate (CI runs all three): `uv run ruff check && uv run ruff format --check && uv run ty check`. Docker must be running; do **not** set `TEST_DATABASE_URL` locally.
- Tests build the schema with `BaseDbModel.metadata.create_all` (`backend/tests/conftest.py:71`), not migrations: every schema property must be declared on the model **and** in the migration (Task 1 checks both).

---

- **SUPERSEDED IN PART BY THE SHIPPED CODE (commit `b295ec7a`) — read before any task below.** The
  tasks define `_lock_user_app`, `_read_fresh` and `_read_successor` on `RefreshTokenService`. They
  shipped on `RefreshTokenRepository` as `lock_user_app`, `get_by_id` and `get_successor`, joined by
  `mark_revoked` (replacing the inline `token.revoked_at = self._now()` in `_refresh_sdk`) and
  `revoke_live_sdk_tokens` (replacing the mint's inline `update(...)`), because
  `backend/AGENTS.md:297` reserves database operations for repositories. None of the five commits:
  each SDK path is still ONE transaction, closed by a single commit that `repo.create` or
  `repo.revoke_token` issues on a path that writes and the service issues on a path that writes
  nothing, because `pg_advisory_xact_lock` is held only for that transaction. **Where any task body,
  code listing, drill step or test snippet below names those five operations, the shipped code
  wins** — read `backend/app/repositories/refresh_token_repository.py` and
  `backend/app/services/refresh_token_service.py`. Everything else in this plan stands as written.

## Task 0: Setup (not a reviewed task)

- [ ] **Step 1: Rename the branch**

```bash
W=/private/tmp/claude-501/-Users-dragan-Desktop-AI-Agent-Calibra/3f480eb8-3f6f-48a0-978f-b798a6ae16b3/scratchpad/ow-refresh-grace
git -C "$W" branch -m docs/sdk-refresh-token-grace fix/sdk-refresh-token-grace
git -C "$W" status --short
```
Expected: `?? docs/superpowers/specs/…`, `?? docs/superpowers/specs/queries/`, `?? docs/superpowers/plans/`, `?? scripts/`. `scripts/` is a local lint copy — never stage it.

- [ ] **Step 2: Local test config** (gitignored; skip if present)

```bash
cd "$W/backend"
test -f config/.env || printf 'ENV=test\nSECRET_KEY=test-secret-key-for-ci\nMASTER_KEY=dGVzdC1tYXN0ZXIta2V5LWZvci10ZXN0aW5nLW9ubHk=\n' > config/.env
export PATH="$HOME/.local/bin:$PATH" && uv sync --group dev
```

- [ ] **Step 3: Baseline (must be green before any change)**

```bash
uv run pytest tests/api/v1/test_token.py tests/services/test_refresh_token_service.py tests/repositories/test_refresh_token_repository.py -v --no-cov
```
Expected: all PASS. If anything fails here, stop and report — a red baseline makes every later drill meaningless.

- [ ] **Step 4: Commit the spec, query and plan**

```bash
cd "$W"
git add docs/superpowers/specs/2026-09-11-sdk-refresh-token-lost-rotation-design.md \
        docs/superpowers/specs/queries/2026-09-11-sdk-refresh-stuck-clients.kql \
        docs/superpowers/plans/2026-09-11-sdk-refresh-token-lost-rotation.md
git commit -m "docs(auth): spec and plan for SDK refresh-token rotation that survives a lost reply" \
  -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GtCGV8A1bgi4FKQX7hkAQb"
```

---

### Task 1: `rotated_from` column (model + migration)

**Files:**
- Modify: `backend/app/models/refresh_token.py:1-30`
- Create: `backend/migrations/versions/2026_09_11_1200-7c3e9a41d2b8_refresh_token_rotated_from.py`
- Test: `backend/tests/repositories/test_refresh_token_repository.py` (append a class)

**Interfaces:**
- Produces: `RefreshToken.rotated_from: str | None` — unique, FK `refresh_token.id` `ON DELETE SET NULL`; set only on a successor, naming the token it replaced (spec §5.1, INV-02).

- [ ] **Step 1: Write the failing test** — append to `backend/tests/repositories/test_refresh_token_repository.py`

```python
import pytest
from sqlalchemy.exc import IntegrityError


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
```
Move the two new imports to the top of the file with the existing ones (ruff isort).

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/repositories/test_refresh_token_repository.py::TestRotatedFromLink -v --no-cov`
Expected: FAIL — `TypeError: 'rotated_from' is an invalid keyword argument for RefreshToken`.

- [ ] **Step 3: Add the column to the model** — `backend/app/models/refresh_token.py`

```python
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import FKDeveloper, FKUser, Indexed, PrimaryKey, str_64
from app.schemas.auth import TokenType


class RefreshToken(BaseDbModel):
    """Generic refresh token for SDK and Developer tokens.

    Stores opaque refresh tokens in the database for secure token refresh.
    The token_type field indicates whether this is an SDK token or Developer token.
    """

    __tablename__ = "refresh_token"

    id: Mapped[PrimaryKey[str_64]]  # rt-{32 hex chars}
    token_type: Mapped[TokenType]

    # For SDK tokens
    user_id: Mapped[Indexed[FKUser] | None]
    app_id: Mapped[str_64 | None]

    # For Developer tokens
    developer_id: Mapped[Indexed[FKDeveloper] | None]

    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    # Set on a successor only: the token this one replaced by rotation. Unique, so one token can
    # never have two successors (fork spec 2026-09-11, INV-02). Null on every minted token.
    rotated_from: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("refresh_token.id", ondelete="SET NULL"), unique=True, nullable=True
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/repositories/test_refresh_token_repository.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 5: Write the migration** — `backend/migrations/versions/2026_09_11_1200-7c3e9a41d2b8_refresh_token_rotated_from.py`

```python
"""refresh_token_rotated_from

Revision ID: 7c3e9a41d2b8
Revises: 9f0940493a9b

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c3e9a41d2b8"
down_revision: Union[str, None] = "9f0940493a9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refresh_token", sa.Column("rotated_from", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        "refresh_token_rotated_from_fkey",
        "refresh_token",
        "refresh_token",
        ["rotated_from"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint("refresh_token_rotated_from_key", "refresh_token", ["rotated_from"])


def downgrade() -> None:
    op.drop_constraint("refresh_token_rotated_from_key", "refresh_token", type_="unique")
    op.drop_constraint("refresh_token_rotated_from_fkey", "refresh_token", type_="foreignkey")
    op.drop_column("refresh_token", "rotated_from")
```

- [ ] **Step 6: Verify the migration against a real PostgreSQL 16 (the Azure major)**

```bash
docker run -d --rm --name ow-migr -e POSTGRES_USER=open-wearables -e POSTGRES_PASSWORD=open-wearables \
  -e POSTGRES_DB=open-wearables -p 55432:5432 postgres:16
sleep 5
export DB_HOST=localhost DB_PORT=55432
uv run alembic upgrade head
uv run alembic check          # expected: "No new upgrade operations detected."
uv run alembic downgrade -1 && uv run alembic upgrade head
```
Expected: upgrade, check, downgrade and re-upgrade all succeed.

- [ ] **Step 7: Mutation drills — both sides of the model/migration agreement** (spec: an agreement guard is proved from both sides)
  1. Remove `unique=True` from the model → run Step 4's test → **red** (no IntegrityError). Also `uv run alembic check` → reports a removed unique constraint. Restore.
  2. Remove the `op.create_unique_constraint(...)` line from the migration → `uv run alembic downgrade base && uv run alembic upgrade head && uv run alembic check` → **reports an added unique constraint**. Restore; `downgrade base && upgrade head && check` clean again.
  Then `docker stop ow-migr` and `unset DB_HOST DB_PORT`.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check && uv run ruff format --check && uv run ty check
cd .. && git add backend/app/models/refresh_token.py backend/migrations/versions/2026_09_11_1200-7c3e9a41d2b8_refresh_token_rotated_from.py backend/tests/repositories/test_refresh_token_repository.py
git commit -m "fix(auth): link each SDK refresh-token successor to the token it replaced" -m "<drill results from Step 7>" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GtCGV8A1bgi4FKQX7hkAQb"
```

---

### Task 2: `sdk_refresh_grace_seconds` setting

**Files:**
- Modify: `backend/app/config.py:61-65` (add field), and add a validator next to `_parse_pull_sync_lookback` (`backend/app/config.py:277`)
- Test: `backend/tests/test_config_sdk_refresh_grace.py` (create)

**Interfaces:**
- Produces: `settings.sdk_refresh_grace_seconds: int` (default `604800`, always `> 0`).

- [ ] **Step 1: Write the failing test** — `backend/tests/test_config_sdk_refresh_grace.py`

```python
"""The SDK refresh-token grace window is a validated setting (fork spec 2026-09-11, D-05)."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_default_is_seven_days() -> None:
    assert Settings(secret_key="test").sdk_refresh_grace_seconds == 604800


@pytest.mark.parametrize("value", [0, -1, "7d", "P7D"])
def test_non_positive_or_unparsable_value_fails_startup(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(secret_key="test", sdk_refresh_grace_seconds=value)  # ty: ignore[invalid-argument-type]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_config_sdk_refresh_grace.py -v --no-cov`
Expected: `test_default_is_seven_days` FAILS with `AttributeError`; the parametrized cases FAIL (no ValidationError is raised for an unknown field, since `extra="ignore"`).

- [ ] **Step 3: Add the field and validator** — in `backend/app/config.py`, under `# AUTH SETTINGS` after `token_lifetime: int = 3600`:

```python
    # How long a superseded SDK refresh token may still be exchanged for its unused successor
    # (fork spec 2026-09-11, D-05). Positive whole seconds; default 7 days (Q-01).
    sdk_refresh_grace_seconds: int = 604800
```

and next to `_parse_pull_sync_lookback`:

```python
    @field_validator("sdk_refresh_grace_seconds")
    @classmethod
    def _validate_sdk_refresh_grace_seconds(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("SDK_REFRESH_GRACE_SECONDS must be a positive number of seconds")
        return v
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_config_sdk_refresh_grace.py -v --no-cov`
Expected: PASS (5 tests).

- [ ] **Step 5: Mutation drill** — delete the validator → the `0` and `-1` cases go **red** (the `"7d"`/`"P7D"` cases stay green: pydantic's int parsing rejects them on its own). Restore.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check && uv run ruff format --check && uv run ty check
cd .. && git add backend/app/config.py backend/tests/test_config_sdk_refresh_grace.py
git commit -m "fix(auth): add validated sdk_refresh_grace_seconds setting" -m "<drill result>" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GtCGV8A1bgi4FKQX7hkAQb"
```

---

### Task 3: Serialised, linked SDK rotation (no grace yet)

**Files:**
- Modify: `backend/app/services/refresh_token_service.py` (whole `refresh_token` method, `create_sdk_refresh_token` signature, new helpers)
- Test: `backend/tests/services/test_refresh_token_service.py` (append classes)

**Interfaces:**
- Consumes: `RefreshToken.rotated_from` (Task 1).
- Produces (later tasks call these):
  - `RefreshTokenService._now: Callable[[], datetime]` — injectable clock, default `datetime.now(timezone.utc)`.
  - `RefreshTokenService._lock_user_app(db_session: DbSession, user_id: UUID, app_id: str) -> None`
  - `RefreshTokenService._read_fresh(db_session: DbSession, token_id: str) -> RefreshToken | None`
  - `RefreshTokenService._unauthorized() -> HTTPException`
  - `RefreshTokenService._sdk_token_response(user_id: UUID, app_id: str, refresh_token: str) -> TokenResponse`
  - `RefreshTokenService.create_sdk_refresh_token(db_session, user_id, app_id, rotated_from: str | None = None) -> str`
  - `RefreshTokenService._refresh_sdk(db_session: DbSession, first_read: RefreshToken) -> TokenResponse`

- [ ] **Step 1: Write the failing tests** — append to `backend/tests/services/test_refresh_token_service.py` (add `from sqlalchemy import delete, select` to the imports)

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/services/test_refresh_token_service.py -k "Rotation or DeveloperRefresh" -v --no-cov`
Expected: `test_rotation_links…` FAILS (`rotated_from` is `None`); the lock tests FAIL with `AttributeError: … has no attribute '_lock_user_app'`; `test_row_deleted…` fails the same way. The first developer test PASSES already (it pins today's behaviour).

- [ ] **Step 3: Implement** — in `backend/app/services/refresh_token_service.py`:

Imports (replace lines 1-14):

```python
import secrets
from collections.abc import Callable
from datetime import datetime, timezone
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
```

Constructor:

```python
    def __init__(self, log: Logger, now: Callable[[], datetime] | None = None) -> None:
        self.logger = log
        self.repo = refresh_token_repository
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))
```

`create_sdk_refresh_token` gains a keyword and passes it to the row (keep the rest of the method):

```python
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
```

New helpers (after `_generate_refresh_token_id`):

```python
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
```

Replace the whole `refresh_token` method with the dispatcher, the SDK branch, and today's code moved verbatim (minus its now-unreachable SDK arm) into `_refresh_non_sdk`:

```python
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
        db_session.commit()
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
```

- [ ] **Step 4: Run to verify they pass, plus the existing token tests**

Run: `uv run pytest tests/services/test_refresh_token_service.py tests/api/v1/test_token.py -v --no-cov`
Expected: all PASS (including the unchanged `test_refresh_token_rotation_invalidates_old_token` — grace does not exist yet).

- [ ] **Step 5: Mutation drills**
  1. Pass `rotated_from=None` in `_refresh_sdk` → `test_rotation_links…` **red**. Restore.
  2. Delete the `self._lock_user_app(...)` call → `test_sdk_refresh_takes_the_user_app_lock` **red**. Restore.
  3. Replace the re-read `token = self._read_fresh(db_session, token_id)` with `token = first_read` → `test_row_deleted_between_first_read_and_lock_is_rejected` **red**: the stale object still looks live, so the code rotates it and the successor insert fails its foreign key to the deleted row (`IntegrityError`, not a 401). Restore.
  4. Route developer tokens through `_refresh_sdk` → `test_developer_refresh_takes_no_lock` **red**. Restore.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check && uv run ruff format --check && uv run ty check
cd .. && git add backend/app/services/refresh_token_service.py backend/tests/services/test_refresh_token_service.py
git commit -m "fix(auth): serialise SDK refresh-token rotation per user and app, and link successors" -m "<drill results>" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GtCGV8A1bgi4FKQX7hkAQb"
```

---

### Task 4: Grace for a superseded SDK token

**Files:**
- Modify: `backend/app/services/refresh_token_service.py` (`_refresh_sdk` revoked branch, new `_read_successor`)
- Modify: `backend/tests/api/v1/test_token.py:122-154` (contract change, spec §5.4)
- Test: `backend/tests/services/test_refresh_token_service.py` (append), `backend/tests/api/v1/test_token.py` (append developer test)
- Create: `backend/tests/integrations/sdk_refresh_race_support.py`, `backend/tests/integrations/test_sdk_refresh_concurrency.py`

**Interfaces:**
- Consumes: Task 3 helpers, `settings.sdk_refresh_grace_seconds` (Task 2).
- Produces:
  - `RefreshTokenService._read_successor(db_session: DbSession, token_id: str) -> RefreshToken | None`
  - Test support (used by Tasks 5 and 6): `PausedCommit(monkeypatch, session)` with `.reached` / `.release` events; `run_in_thread(fn) -> (thread, result_dict, done_event)`; `backend_pid(session) -> int`; `wait_until_blocked_or_done(factory, pid, done) -> None`; fixture `committed_user` and helper `sdk_rows(factory, user_id)` in the concurrency test module.

- [ ] **Step 1: Write the failing service tests** — append to `backend/tests/services/test_refresh_token_service.py` (add `from datetime import datetime, timedelta, timezone` and `from app.config import settings`)

```python
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
```

- [ ] **Step 2: Rewrite the upstream contract test and add its developer twin** — in `backend/tests/api/v1/test_token.py`, replace `test_refresh_token_rotation_invalidates_old_token` (lines 122-154) with:

```python
    def test_refresh_token_rotation_invalidates_old_token(
        self, client: TestClient, db: Session, api_v1_prefix: str
    ) -> None:
        """An SDK token stops working once its successor has been used (fork spec D-05).

        Upstream rejected it straight after rotation; a reply the phone never applied then
        locked the phone out for good. The old token now keeps its successor until that is used.
        """
        user = UserFactory()
        old_refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")

        first = client.post(f"{api_v1_prefix}/token/refresh", json={"refresh_token": old_refresh_token})
        assert first.status_code == 200
        new_refresh_token = first.json()["refresh_token"]

        second = client.post(f"{api_v1_prefix}/token/refresh", json={"refresh_token": new_refresh_token})
        assert second.status_code == 200

        response = client.post(f"{api_v1_prefix}/token/refresh", json={"refresh_token": old_refresh_token})
        assert response.status_code == 401

    def test_developer_refresh_token_rotation_invalidates_old_token(
        self, client: TestClient, db: Session, api_v1_prefix: str
    ) -> None:
        """Developer tokens keep upstream's strict rotation (fork spec D-04)."""
        developer = DeveloperFactory()
        old_refresh_token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        first = client.post(f"{api_v1_prefix}/token/refresh", json={"refresh_token": old_refresh_token})
        assert first.status_code == 200

        response = client.post(f"{api_v1_prefix}/token/refresh", json={"refresh_token": old_refresh_token})
        assert response.status_code == 401
```

- [ ] **Step 3: Run to verify the grace tests fail**

Run: `uv run pytest tests/services/test_refresh_token_service.py -k Grace tests/api/v1/test_token.py -v --no-cov`
Expected: the two "gets the same successor"/"idempotent" tests and the inclusive-boundary test FAIL with 401; the rejection tests PASS already (they pin behaviour the grace must not loosen); both rotation tests in `test_token.py` PASS.

- [ ] **Step 4: Implement grace** — in `refresh_token_service.py`, add `timedelta` to the datetime import, add the helper, and replace the last two lines of `_refresh_sdk` (`db_session.commit()` / `raise self._unauthorized()`):

```python
    @staticmethod
    def _read_successor(db_session: DbSession, token_id: str) -> RefreshToken | None:
        """The row that replaced `token_id` by rotation, if any (fork spec D-03), read fresh."""
        stmt = (
            select(RefreshToken)
            .where(RefreshToken.rotated_from == token_id)
            .execution_options(populate_existing=True)
        )
        return db_session.execute(stmt).scalar_one_or_none()
```

```python
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
```
(`reason` is logged at debug for now; Task 7 turns it into the D-08 line.)

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/services/test_refresh_token_service.py tests/api/v1/test_token.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 6: Write the concurrency support module** — `backend/tests/integrations/sdk_refresh_race_support.py`

```python
"""Two-connection helpers for SDK refresh-token race tests (fork spec 2026-09-11, §5.4).

The `db` fixture runs a test inside one connection and a savepoint, and one connection cannot contend
with itself for a lock. These helpers use real sessions from `session_factory`, so both sides commit
for real. One side is paused at its commit (after flushing its writes) and resumes only once the other
side is observed waiting on a lock or has finished — so no outcome depends on timing.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


class PausedCommit:
    """Make `session.commit()` flush its writes, then wait for `release` before committing."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        original_commit = session.commit

        def paused_commit() -> None:
            session.flush()
            self.reached.set()
            if not self.release.wait(timeout=15):
                raise TimeoutError("paused commit was never released")
            original_commit()

        monkeypatch.setattr(session, "commit", paused_commit)


def run_in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any], threading.Event]:
    """Run `fn` in a thread; its return value lands in result["value"], an exception in result["error"]."""
    result: dict[str, Any] = {}
    done = threading.Event()

    def target() -> None:
        try:
            result["value"] = fn()
        except Exception as exc:  # the test asserts on it
            result["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result, done


def backend_pid(session: Session) -> int:
    return int(session.execute(text("SELECT pg_backend_pid()")).scalar_one())


def wait_until_blocked_or_done(
    factory: sessionmaker, pid: int, done: threading.Event, timeout: float = 10.0
) -> None:
    """Return once backend `pid` waits on a lock (with the guard in place) or has finished (without it)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done.is_set():
            return
        with factory() as probe:
            wait_type = probe.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
            ).scalar_one_or_none()
        if wait_type == "Lock":
            return
        time.sleep(0.02)
    raise AssertionError(f"backend {pid} neither waited on a lock nor finished within {timeout}s")
```

- [ ] **Step 7: Write the concurrency tests** — `backend/tests/integrations/test_sdk_refresh_concurrency.py`

```python
"""Concurrency guarantees of SDK refresh-token rotation (fork spec 2026-09-11, D-06, INV-02, §5.3)."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.models import RefreshToken, User
from app.services.refresh_token_service import refresh_token_service
from tests.integrations.sdk_refresh_race_support import (
    PausedCommit,
    backend_pid,
    run_in_thread,
    wait_until_blocked_or_done,
)

APP_ID = "race-test-app"


@pytest.fixture
def committed_user(session_factory: sessionmaker) -> Iterator[UUID]:
    """A user committed for real so two connections see it; deleting it afterwards cascades its tokens."""
    user_id = uuid4()
    with session_factory() as session:
        session.add(User(id=user_id, email=f"race-{user_id}@example.test"))
        session.commit()
    yield user_id
    with session_factory() as session:
        session.execute(delete(User).where(User.id == user_id))
        session.commit()


def sdk_rows(factory: sessionmaker, user_id: UUID) -> list[RefreshToken]:
    with factory() as session:
        return list(session.execute(select(RefreshToken).where(RefreshToken.user_id == user_id)).scalars())


def test_two_concurrent_refreshes_of_one_live_token_share_one_successor(
    session_factory: sessionmaker, committed_user: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory() as setup:
        t0 = refresh_token_service.create_sdk_refresh_token(setup, committed_user, APP_ID)
    a, b = session_factory(), session_factory()
    try:
        paused = PausedCommit(monkeypatch, a)
        thread_a, result_a, _ = run_in_thread(lambda: refresh_token_service.refresh_token(a, t0))
        assert paused.reached.wait(timeout=10), "A never reached its commit"
        pid_b = backend_pid(b)
        thread_b, result_b, done_b = run_in_thread(lambda: refresh_token_service.refresh_token(b, t0))
        wait_until_blocked_or_done(session_factory, pid_b, done_b)
        paused.release.set()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)
    finally:
        a.close()
        b.close()

    assert "error" not in result_a, result_a.get("error")
    assert "error" not in result_b, result_b.get("error")
    assert result_a["value"].refresh_token == result_b["value"].refresh_token
    assert [r.id for r in sdk_rows(session_factory, committed_user) if r.rotated_from == t0] == [
        result_a["value"].refresh_token
    ]


def test_grace_refresh_waits_for_a_concurrent_rotation_of_the_successor(
    session_factory: sessionmaker, committed_user: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory() as setup:
        t0 = refresh_token_service.create_sdk_refresh_token(setup, committed_user, APP_ID)
        t1 = refresh_token_service.refresh_token(setup, t0).refresh_token
    a, b = session_factory(), session_factory()  # A presents T0 (grace); B rotates T1
    try:
        paused = PausedCommit(monkeypatch, b)
        thread_b, result_b, _ = run_in_thread(lambda: refresh_token_service.refresh_token(b, t1))
        assert paused.reached.wait(timeout=10), "B never reached its commit"
        pid_a = backend_pid(a)
        thread_a, result_a, done_a = run_in_thread(lambda: refresh_token_service.refresh_token(a, t0))
        wait_until_blocked_or_done(session_factory, pid_a, done_a)
        paused.release.set()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)
    finally:
        a.close()
        b.close()

    assert "error" not in result_b, result_b.get("error")
    error = result_a.get("error")
    assert isinstance(error, HTTPException) and error.status_code == 401, result_a
```

- [ ] **Step 8: Run the concurrency tests**

Run: `uv run pytest tests/integrations/test_sdk_refresh_concurrency.py -v --no-cov`
Expected: both PASS.

- [ ] **Step 9: Mutation drills**
  1. Replace the `if successor is None: reason = "revoked"` arm so a missing successor falls through to grace → `test_token_revoked_while_live_gets_no_grace` and `test_refresh_revoked_token` (`tests/api/v1/test_token.py:94`) **red** (500 from `None.revoked_at`). Restore.
  2. Drop the `successor.revoked_at is not None` arm → both "rejected once its successor was …" tests **red**. Restore.
  3. Change `>` to `>=` in the age check → `test_grace_boundary_is_inclusive` **red** (at-cap case). Delete the age check → same test **red** (past-cap case). Restore.
  4. In the grace branch, return a freshly minted successor instead (`self.create_sdk_refresh_token(db_session, user_id, app_id)`) → `test_grace_is_idempotent_and_writes_no_rows` **red**. Restore.
  5. Delete the `self._lock_user_app(...)` call in `_refresh_sdk` → both concurrency tests **red** (first: B raises `IntegrityError` on the unique `rotated_from`; second: A returns 200 with a token B is revoking). Restore.
  Before concluding a drill is red, confirm the rest of the suite is green with the code restored (a red baseline makes a drill lie).

- [ ] **Step 10: Lint and commit**

```bash
uv run ruff check && uv run ruff format --check && uv run ty check
cd .. && git add backend/app/services/refresh_token_service.py backend/tests/services/test_refresh_token_service.py \
  backend/tests/api/v1/test_token.py backend/tests/integrations/sdk_refresh_race_support.py \
  backend/tests/integrations/test_sdk_refresh_concurrency.py
git commit -m "fix(auth): let a superseded SDK refresh token reclaim its unused successor" -m "<drill results>" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GtCGV8A1bgi4FKQX7hkAQb"
```

---

### Task 5: Revoke reaches an unrevoked successor (INV-03)

**Files:**
- Modify: `backend/app/services/refresh_token_service.py` (`revoke_token`)
- Test: `backend/tests/services/test_refresh_token_service.py` (append), `backend/tests/integrations/test_sdk_refresh_concurrency.py` (append)

**Interfaces:**
- Consumes: `_lock_user_app`, `_read_fresh`, `_read_successor` (Tasks 3-4); race support (Task 4).
- Produces: `revoke_token(db_session, refresh_token_str) -> bool` with INV-03 semantics for SDK tokens.

- [ ] **Step 1: Write the failing tests** — append to `test_refresh_token_service.py`

```python
class TestSdkRevokeReachesUnrevokedSuccessor:
    """INV-03 (fork spec 2026-09-11)."""

    def test_revoking_a_superseded_token_revokes_its_unrevoked_successor(self, db: Session) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        successor = refresh_token_service.refresh_token(db, old).refresh_token

        assert refresh_token_service.revoke_token(db, old) is True

        row = db.execute(select(RefreshToken).where(RefreshToken.id == successor)).scalar_one()
        assert row.revoked_at is not None
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, old)
        assert exc_info.value.status_code == 401

    def test_revoking_a_superseded_token_whose_successor_is_revoked_is_not_found(self, db: Session) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        successor = refresh_token_service.refresh_token(db, old).refresh_token
        refresh_token_service.refresh_token(db, successor)  # successor rotated → revoked

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, old)

        assert exc_info.value.status_code == 404

    def test_revoke_where_row_is_deleted_between_first_read_and_lock_is_not_found(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user = UserFactory()
        token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        real_lock = refresh_token_service._lock_user_app

        def lock_then_delete(session: Session, user_id: object, app_id: str) -> None:
            real_lock(session, user_id, app_id)  # ty: ignore[invalid-argument-type]
            session.execute(delete(RefreshToken).where(RefreshToken.id == token))

        monkeypatch.setattr(refresh_token_service, "_lock_user_app", lock_then_delete)

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, token)

        assert exc_info.value.status_code == 404

    def test_revoking_a_rotated_developer_token_is_not_found(self, db: Session) -> None:
        developer = DeveloperFactory()
        old = refresh_token_service.create_developer_refresh_token(db, developer.id)
        refresh_token_service.refresh_token(db, old)

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, old)

        assert exc_info.value.status_code == 404
```

Append to `test_sdk_refresh_concurrency.py`:

```python
def test_revoke_waits_for_a_concurrent_rotation_and_reaches_the_successor(
    session_factory: sessionmaker, committed_user: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory() as setup:
        t0 = refresh_token_service.create_sdk_refresh_token(setup, committed_user, APP_ID)
    a, b = session_factory(), session_factory()  # A rotates T0; B revokes T0
    try:
        paused = PausedCommit(monkeypatch, a)
        thread_a, result_a, _ = run_in_thread(lambda: refresh_token_service.refresh_token(a, t0))
        assert paused.reached.wait(timeout=10), "A never reached its commit"
        pid_b = backend_pid(b)
        thread_b, result_b, done_b = run_in_thread(lambda: refresh_token_service.revoke_token(b, t0))
        wait_until_blocked_or_done(session_factory, pid_b, done_b)
        paused.release.set()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)
    finally:
        a.close()
        b.close()

    assert "error" not in result_a, result_a.get("error")
    assert "error" not in result_b, result_b.get("error")
    assert all(r.revoked_at is not None for r in sdk_rows(session_factory, committed_user))
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/services/test_refresh_token_service.py -k Revoke tests/integrations/test_sdk_refresh_concurrency.py -v --no-cov`
Expected: the successor-revocation test FAILS (404); the deleted-row test FAILS (revoke takes no lock yet, so the patched `_lock_user_app` never runs, nothing is deleted, and the call answers 204 instead of raising 404); the concurrency revoke test FAILS (T1 stays live). The 404 and developer tests PASS already (pins).

- [ ] **Step 3: Implement** — replace `revoke_token`:

```python
    def revoke_token(self, db_session: DbSession, refresh_token_str: str) -> bool:
        """Revoke a refresh token.

        For an SDK token that was rotated and whose successor is unrevoked, the successor is revoked
        instead (fork spec INV-03): otherwise a phone that missed a rotation reply and then logged out
        would leave its successor live. Developer tokens keep upstream behaviour (D-04).

        Raises:
            HTTPException: 404 if there is nothing to revoke
        """
        first_read = self._read_fresh(db_session, refresh_token_str)
        if first_read is None:
            raise self._not_found()
        if first_read.token_type != TokenType.SDK:
            return self._revoke_non_sdk(db_session, refresh_token_str)
        token_id = first_read.id
        self._lock_user_app(db_session, first_read.user_id, first_read.app_id)  # ty:ignore[invalid-argument-type]
        token = self._read_fresh(db_session, token_id)
        if token is None:
            db_session.commit()
            raise self._not_found()
        if token.revoked_at is None:
            self.repo.revoke_token(db_session, token)  # commits
            return True
        successor = self._read_successor(db_session, token_id)
        if successor is None or successor.revoked_at is not None:
            db_session.commit()
            raise self._not_found()
        self.repo.revoke_token(db_session, successor)  # commits
        return True

    @staticmethod
    def _not_found() -> HTTPException:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refresh token not found")

    def _revoke_non_sdk(self, db_session: DbSession, refresh_token_str: str) -> bool:
        """Upstream behaviour for developer tokens, unchanged (fork spec D-04)."""
        token = self.repo.get_valid_token(db_session, refresh_token_str)
        if not token:
            raise self._not_found()
        self.repo.revoke_token(db_session, token)
        self.logger.debug(f"Revoked refresh token {refresh_token_str[:10]}...")
        return True
```

- [ ] **Step 4: Run to verify they pass, with the pinned API tests**

Run: `uv run pytest tests/services/test_refresh_token_service.py tests/api/v1/test_token.py tests/integrations/test_sdk_refresh_concurrency.py -v --no-cov`
Expected: all PASS — including `test_revoke_token_success` (`:156`), `test_revoke_token_not_found` (`:178`), `test_revoke_already_revoked_token` (`:189`).

- [ ] **Step 5: Mutation drills**
  1. Remove the successor branch (answer 404 for any revoked token) → `test_revoking_a_superseded_token_revokes…` **red**. Restore.
  2. Revoke the successor unconditionally and return 204 → `test_revoking_a_superseded_token_whose_successor_is_revoked_is_not_found` **red**. Restore.
  3. Replace the re-read with `token = first_read` → the deleted-row test **red** (500 or 204). Restore.
  4. Delete the `_lock_user_app` call in `revoke_token` → the concurrency revoke test **red** (T1 stays live). Restore.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check && uv run ruff format --check && uv run ty check
cd .. && git add backend/app/services/refresh_token_service.py backend/tests/services/test_refresh_token_service.py backend/tests/integrations/test_sdk_refresh_concurrency.py
git commit -m "fix(auth): revoking a superseded SDK token also revokes its unused successor" -m "<drill results>" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GtCGV8A1bgi4FKQX7hkAQb"
```

---

### Task 6: A fresh mint revokes earlier chains for the same user and app (D-11)

**Files:**
- Modify: `backend/app/services/refresh_token_service.py` (new `mint_sdk_refresh_token`)
- Modify: `backend/app/api/routes/v1/sdk_token.py:71`
- Test: `backend/tests/services/test_refresh_token_service.py` (append), `backend/tests/api/v1/test_token.py` (append), `backend/tests/integrations/test_sdk_refresh_concurrency.py` (append)

**Interfaces:**
- Consumes: `_lock_user_app`, `create_sdk_refresh_token`, `_now`.
- Produces: `RefreshTokenService.mint_sdk_refresh_token(db_session: DbSession, user_id: UUID, app_id: str) -> str`.

- [ ] **Step 1: Write the failing tests** — append to `test_refresh_token_service.py`

```python
class TestSdkMintRevokesEarlierChains:
    """D-11 (fork spec 2026-09-11): one phone per account."""

    def test_mint_revokes_earlier_live_tokens_for_the_same_user_and_app(self, db: Session) -> None:
        user = UserFactory()
        earlier = refresh_token_service.create_sdk_refresh_token(db, user.id, "calibra_app")
        other_app = refresh_token_service.create_sdk_refresh_token(db, user.id, "other_app")
        developer = DeveloperFactory()
        developer_token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        minted = refresh_token_service.mint_sdk_refresh_token(db, user.id, "calibra_app")

        def revoked_at(token_id: str) -> object:
            return db.execute(select(RefreshToken.revoked_at).where(RefreshToken.id == token_id)).scalar_one()

        assert revoked_at(earlier) is not None
        assert revoked_at(minted) is None
        assert revoked_at(other_app) is None
        assert revoked_at(developer_token) is None

    def test_superseded_token_is_rejected_once_a_mint_revoked_its_successor(self, db: Session) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, "calibra_app")
        refresh_token_service.refresh_token(db, old)  # old → successor (unapplied by the phone)

        refresh_token_service.mint_sdk_refresh_token(db, user.id, "calibra_app")  # reconnect

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, old)
        assert exc_info.value.status_code == 401
```

Append to `tests/api/v1/test_token.py` inside `TestRefreshToken` (the file already imports `ApplicationFactory`, `DeveloperFactory` and `UserFactory`):

```python
    def test_second_mint_for_same_user_and_app_revokes_the_first_token(
        self, client: TestClient, db: Session, api_v1_prefix: str
    ) -> None:
        """POST /users/{id}/token revokes earlier SDK chains for that user and app (fork spec D-11)."""
        developer = DeveloperFactory()
        application = ApplicationFactory(developer=developer, app_secret="test_app_secret")
        user = UserFactory()
        credentials = {"app_id": application.app_id, "app_secret": "test_app_secret"}

        first = client.post(f"{api_v1_prefix}/users/{user.id}/token", json=credentials)
        assert first.status_code == 200
        second = client.post(f"{api_v1_prefix}/users/{user.id}/token", json=credentials)
        assert second.status_code == 200

        response = client.post(
            f"{api_v1_prefix}/token/refresh", json={"refresh_token": first.json()["refresh_token"]}
        )
        assert response.status_code == 401
```

Append to `test_sdk_refresh_concurrency.py`:

```python
def test_mint_waits_for_a_concurrent_rotation_and_leaves_only_its_own_token_live(
    session_factory: sessionmaker, committed_user: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory() as setup:
        t0 = refresh_token_service.create_sdk_refresh_token(setup, committed_user, APP_ID)
    a, b = session_factory(), session_factory()  # A rotates T0; B mints
    try:
        paused = PausedCommit(monkeypatch, a)
        thread_a, result_a, _ = run_in_thread(lambda: refresh_token_service.refresh_token(a, t0))
        assert paused.reached.wait(timeout=10), "A never reached its commit"
        pid_b = backend_pid(b)
        thread_b, result_b, done_b = run_in_thread(
            lambda: refresh_token_service.mint_sdk_refresh_token(b, committed_user, APP_ID)
        )
        wait_until_blocked_or_done(session_factory, pid_b, done_b)
        paused.release.set()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)
    finally:
        a.close()
        b.close()

    assert "error" not in result_a, result_a.get("error")
    assert "error" not in result_b, result_b.get("error")
    live = [r.id for r in sdk_rows(session_factory, committed_user) if r.revoked_at is None]
    assert live == [result_b["value"]]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/services/test_refresh_token_service.py -k Mint tests/api/v1/test_token.py -k mint tests/integrations/test_sdk_refresh_concurrency.py -k mint -v --no-cov`
Expected: FAIL — `AttributeError: … has no attribute 'mint_sdk_refresh_token'`; the API test FAILS (the first token still refreshes: 200).

- [ ] **Step 3: Implement** — in `refresh_token_service.py`, change the sqlalchemy import to `from sqlalchemy import select, text, update` and add:

```python
    def mint_sdk_refresh_token(self, db_session: DbSession, user_id: UUID, app_id: str) -> str:
        """Fresh SDK mint: revoke the user's earlier live SDK tokens for this app, then create one.

        Calibra is one phone per account (fork spec D-11): a reconnect means "this phone is the one".
        Runs under the (user, app) lock so a concurrent rotation cannot slip a successor past it (D-06).
        """
        self._lock_user_app(db_session, user_id, app_id)
        db_session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.token_type == TokenType.SDK,
                RefreshToken.user_id == user_id,
                RefreshToken.app_id == app_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=self._now())
        )
        return self.create_sdk_refresh_token(db_session, user_id, app_id)  # one commit for both
```

In `backend/app/api/routes/v1/sdk_token.py:71` replace the call:

```python
    refresh_token = refresh_token_service.mint_sdk_refresh_token(db, user_id, app_id)
```

- [ ] **Step 4: Run to verify they pass, and the whole token/sdk-token area**

Run: `uv run pytest tests/services/test_refresh_token_service.py tests/api/v1/test_token.py tests/api/v1/test_sdk_token.py tests/services/test_sdk_token_service.py tests/integrations/test_sdk_refresh_concurrency.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 5: Mutation drills**
  1. Delete the `db_session.execute(update(...))` statement → `test_mint_revokes_earlier…`, the API test and `test_superseded_token_is_rejected_once_a_mint…` **red**. Restore.
  2. Drop `RefreshToken.app_id == app_id` from the filter → `test_mint_revokes_earlier…` **red** (`other_app` revoked). Restore.
  3. Delete the `_lock_user_app` call in `mint_sdk_refresh_token` → the concurrency mint test **red** (T1 stays live). Restore.
  4. Revert `sdk_token.py:71` to `create_sdk_refresh_token` → the API test **red**. Restore.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check && uv run ruff format --check && uv run ty check
cd .. && git add backend/app/services/refresh_token_service.py backend/app/api/routes/v1/sdk_token.py \
  backend/tests/services/test_refresh_token_service.py backend/tests/api/v1/test_token.py backend/tests/integrations/test_sdk_refresh_concurrency.py
git commit -m "fix(auth): a fresh SDK mint revokes the user's earlier tokens for that app" -m "<drill results>" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GtCGV8A1bgi4FKQX7hkAQb"
```

---

### Task 7: Refresh-outcome logs and their consumer (D-08)

**Files:**
- Modify: `backend/app/services/refresh_token_service.py` (constants, `_log_refresh`, call sites in `refresh_token` / `_refresh_sdk`)
- Test: `backend/tests/services/test_refresh_token_logging.py` (create)

**Interfaces:**
- Consumes: the committed query `docs/superpowers/specs/queries/2026-09-11-sdk-refresh-stuck-clients.kql`.
- Produces: module constants `REFRESH_ACTION_ROTATED`, `REFRESH_ACTION_GRACE_REISSUED`, `REFRESH_ACTION_REJECTED`, `REFRESH_REJECT_REASONS: tuple[str, ...]` in `app.services.refresh_token_service`.

- [ ] **Step 1: Write the failing tests** — `backend/tests/services/test_refresh_token_logging.py`

```python
"""Each SDK refresh outcome emits exactly one JSON line the stuck-client query can read (fork spec D-08)."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import settings
from app.models import RefreshToken
from app.services import refresh_token_service as module
from app.services.refresh_token_service import refresh_token_service
from tests.factories import DeveloperFactory, UserFactory

KQL = (
    Path(__file__).resolve().parents[3] / "docs/superpowers/specs/queries/2026-09-11-sdk-refresh-stuck-clients.kql"
).read_text()


def refresh_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if str(line.get("action", "")).startswith("refresh_token_")]


def test_literals_agree_with_the_committed_query() -> None:
    kql_actions = set(re.findall(r"'(refresh_token_[a-z_]+)'", KQL))
    reason_clause = re.search(r"reason in \(([^)]*)\)", KQL)
    assert reason_clause is not None
    kql_reasons = set(re.findall(r"'([a-z_]+)'", reason_clause.group(1)))

    assert kql_actions == {
        module.REFRESH_ACTION_ROTATED,
        module.REFRESH_ACTION_GRACE_REISSUED,
        module.REFRESH_ACTION_REJECTED,
    }
    assert set(module.REFRESH_REJECT_REASONS) == kql_reasons | {"unknown"}


def test_rotation_grace_and_rejections_each_log_one_line(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    user = UserFactory()
    old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
    capsys.readouterr()

    successor = refresh_token_service.refresh_token(db, old).refresh_token
    assert refresh_lines(capsys) == [
        {
            "level": "info",
            "message": "refresh_token_rotated",
            "provider": None,
            "action": "refresh_token_rotated",
            "user_id": str(user.id),
            "token_type": "sdk",
        }
    ]

    refresh_token_service.refresh_token(db, old)
    (grace,) = refresh_lines(capsys)
    assert grace["action"] == "refresh_token_grace_reissued"
    assert grace["user_id"] == str(user.id)
    assert isinstance(grace["rotated_age_seconds"], int)

    refresh_token_service.refresh_token(db, successor)  # successor used
    capsys.readouterr()
    with pytest.raises(HTTPException):
        refresh_token_service.refresh_token(db, old)
    (rejected,) = refresh_lines(capsys)
    assert (rejected["action"], rejected["reason"], rejected["user_id"]) == (
        "refresh_token_rejected",
        "rotated_successor_used",
        str(user.id),
    )


def test_unknown_id_logs_unknown_without_user(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(HTTPException):
        refresh_token_service.refresh_token(db, "rt-" + "f" * 32)
    (line,) = refresh_lines(capsys)
    assert (line["reason"], "user_id" in line) == ("unknown", False)


def test_row_deleted_between_reads_logs_unknown_with_user(
    db: Session, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    user = UserFactory()
    token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
    real_lock = refresh_token_service._lock_user_app

    def lock_then_delete(session: Session, user_id: object, app_id: str) -> None:
        real_lock(session, user_id, app_id)  # ty: ignore[invalid-argument-type]
        session.execute(delete(RefreshToken).where(RefreshToken.id == token))

    monkeypatch.setattr(refresh_token_service, "_lock_user_app", lock_then_delete)
    capsys.readouterr()
    with pytest.raises(HTTPException):
        refresh_token_service.refresh_token(db, token)
    (line,) = refresh_lines(capsys)
    assert (line["reason"], line["user_id"]) == ("unknown", str(user.id))


def test_past_grace_logs_rotated_past_grace(
    db: Session, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    user = UserFactory()
    rotated_at = datetime(2026, 9, 11, 15, 10, 4, tzinfo=timezone.utc)
    old = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
    monkeypatch.setattr(refresh_token_service, "_now", lambda: rotated_at)
    refresh_token_service.refresh_token(db, old)
    past = rotated_at + timedelta(seconds=settings.sdk_refresh_grace_seconds + 1)
    monkeypatch.setattr(refresh_token_service, "_now", lambda: past)
    capsys.readouterr()
    with pytest.raises(HTTPException):
        refresh_token_service.refresh_token(db, old)
    (line,) = refresh_lines(capsys)
    assert (line["reason"], line["user_id"]) == ("rotated_past_grace", str(user.id))


def test_revoked_token_logs_revoked(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    user = UserFactory()
    token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
    refresh_token_service.revoke_token(db, token)
    capsys.readouterr()
    with pytest.raises(HTTPException):
        refresh_token_service.refresh_token(db, token)
    (line,) = refresh_lines(capsys)
    assert line["reason"] == "revoked"


def test_a_refresh_that_raises_logs_nothing(
    db: Session, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    user = UserFactory()
    token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")

    def fail(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("injected database error")

    monkeypatch.setattr(refresh_token_service, "create_sdk_refresh_token", fail)
    capsys.readouterr()
    with pytest.raises(RuntimeError):
        refresh_token_service.refresh_token(db, token)
    assert refresh_lines(capsys) == []


def test_revoke_mint_and_developer_refresh_log_nothing(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    user = UserFactory()
    token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
    developer = DeveloperFactory()
    developer_token = refresh_token_service.create_developer_refresh_token(db, developer.id)
    capsys.readouterr()

    refresh_token_service.revoke_token(db, token)
    refresh_token_service.mint_sdk_refresh_token(db, user.id, "test_app")
    refresh_token_service.refresh_token(db, developer_token)

    assert refresh_lines(capsys) == []
```
Every reason and action in D-08 now has a behavioural test above, plus the literal-agreement test.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/services/test_refresh_token_logging.py -v --no-cov`
Expected: FAIL — `AttributeError: module … has no attribute 'REFRESH_ACTION_ROTATED'`; the line-count tests fail with empty lists. `test_a_refresh_that_raises_logs_nothing` and `test_revoke_mint_and_developer_refresh_log_nothing` PASS already (pins).

- [ ] **Step 3: Implement** — in `refresh_token_service.py` add `from typing import Any` and `from app.utils.structured_logging import log_structured`, then module constants after the imports:

```python
# D-08 (fork spec 2026-09-11): the strings the committed stuck-client query filters on.
REFRESH_ACTION_ROTATED = "refresh_token_rotated"
REFRESH_ACTION_GRACE_REISSUED = "refresh_token_grace_reissued"
REFRESH_ACTION_REJECTED = "refresh_token_rejected"
REFRESH_REJECT_REASONS: tuple[str, ...] = ("unknown", "revoked", "rotated_successor_used", "rotated_past_grace")
```

A method:

```python
    def _log_refresh(self, action: str, **fields: Any) -> None:
        """Exactly one D-08 line per SDK refresh that returns 200 or 401; bare JSON on stdout at info."""
        log_structured(self.logger, "info", action, action=action, **fields)
```

Call sites (each emitted only after the transaction is settled, so a raising request logs nothing):

In `refresh_token`, the unknown first read:

```python
        if first_read is None:
            self._log_refresh(REFRESH_ACTION_REJECTED, reason="unknown")
            raise self._unauthorized()
```

In `_refresh_sdk`:

```python
        if token is None:
            db_session.commit()
            self._log_refresh(REFRESH_ACTION_REJECTED, reason="unknown", user_id=user_id, token_type=TokenType.SDK)
            raise self._unauthorized()
        if token.revoked_at is None:
            token.revoked_at = self._now()
            successor_id = self.create_sdk_refresh_token(
                db_session,
                user_id=user_id,  # ty:ignore[invalid-argument-type]
                app_id=app_id,  # ty:ignore[invalid-argument-type]
                rotated_from=token_id,
            )
            self._log_refresh(REFRESH_ACTION_ROTATED, user_id=user_id, token_type=TokenType.SDK)
            return self._sdk_token_response(user_id, app_id, successor_id)  # ty:ignore[invalid-argument-type]
        successor = self._read_successor(db_session, token_id)
        rotated_age = self._now() - token.revoked_at
        if successor is None:
            reason = "revoked"
        elif successor.revoked_at is not None:
            reason = "rotated_successor_used"
        elif rotated_age > timedelta(seconds=settings.sdk_refresh_grace_seconds):
            reason = "rotated_past_grace"
        else:
            successor_id = successor.id
            db_session.commit()  # grace writes nothing (INV-04); ends the transaction, releasing the lock
            self._log_refresh(
                REFRESH_ACTION_GRACE_REISSUED,
                user_id=user_id,
                token_type=TokenType.SDK,
                rotated_age_seconds=int(rotated_age.total_seconds()),
            )
            return self._sdk_token_response(user_id, app_id, successor_id)  # ty:ignore[invalid-argument-type]
        db_session.commit()
        self._log_refresh(REFRESH_ACTION_REJECTED, reason=reason, user_id=user_id, token_type=TokenType.SDK)
        raise self._unauthorized()
```
(Remove Task 4's temporary `self.logger.debug(f"SDK refresh rejected: {reason}")` line.)

- [ ] **Step 4: Run to verify they pass, plus the whole area**

Run: `uv run pytest tests/services/test_refresh_token_logging.py tests/services/test_refresh_token_service.py tests/api/v1/test_token.py tests/integrations/test_sdk_refresh_concurrency.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 5: Mutation drills — both sides of the query agreement**
  1. Rename `REFRESH_ACTION_ROTATED`'s value in code → `test_literals_agree…` and the rotation line test **red**. Restore.
  2. Rename `'rotated_past_grace'` inside the `.kql` file → `test_literals_agree…` **red**. Restore.
  3. Add a new reason to `REFRESH_REJECT_REASONS` → `test_literals_agree…` **red**. Restore.
  4. Emit through `self.logger.info(...)` instead of `log_structured` → the line tests **red** (no JSON line). Restore.
  5. Move the rotation `_log_refresh` call before `create_sdk_refresh_token` → `test_a_refresh_that_raises_logs_nothing` **red**. Restore.
  6. Drop `user_id=user_id` from the deleted-between-reads call → `test_row_deleted_between_reads_logs_unknown_with_user` **red**. Restore.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check && uv run ruff format --check && uv run ty check
cd .. && git add backend/app/services/refresh_token_service.py backend/tests/services/test_refresh_token_logging.py
git commit -m "fix(auth): log every SDK refresh outcome for the stuck-client query" -m "<drill results>" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GtCGV8A1bgi4FKQX7hkAQb"
```

---

### Task 8: Whole-fix verification and final review

- [ ] **Step 1: Full backend suite, once, and the lint gate**

```bash
cd backend && export PATH="$HOME/.local/bin:$PATH"
uv run pytest -v --no-cov 2>&1 | tail -30
uv run ruff check && uv run ruff format --check && uv run ty check
```
Expected: the summary line reports 0 failed and 0 errors; read the collected count, not just the green line. All three lint commands exit 0.

- [ ] **Step 2: Final whole-fix review — Fable subagent**

Dispatch one reviewer with `model: "fable"` over `git diff origin/release/0.6.2-syn...HEAD` plus the spec. It checks every §5.4 row has a test whose named mutation was drilled (commit bodies), INV-01/INV-03/D-11 match the code, the developer path is behaviourally unchanged, and no ORM attribute is read after a commit inside the service. Fix findings in one pass as a new commit; re-run Step 1.

- [ ] **Step 3: Stop and report to Dragan**

Report: commits on `fix/sdk-refresh-token-grace`, full-suite counts, lint results, drill results per task, and the Fable verdict. **Do not push.** After Dragan approves: push, open a PR into `release/0.6.2-syn` titled `fix(auth): SDK refresh-token rotation survives a lost reply`, and post `@codex review` if that is the fork's practice. After merge (Dragan): tag `0.6.2-syn.7`, bump `OW_REF` in `calibra-ow-deploy`, verify on dev per spec §5.5.
