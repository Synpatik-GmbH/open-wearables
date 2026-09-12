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
from app.services.refresh_token_service import (
    REFRESH_ACTION_GRACE_REISSUED,
    REFRESH_ACTION_REJECTED,
    REFRESH_ACTION_ROTATED,
    REFRESH_REJECT_REASONS,
    refresh_token_service,
)
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
    assert "unknown" not in kql_reasons  # §5.6: the filter leaves `unknown` out, with or without a user_id

    assert kql_actions == {
        REFRESH_ACTION_ROTATED,
        REFRESH_ACTION_GRACE_REISSUED,
        REFRESH_ACTION_REJECTED,
    }
    assert set(REFRESH_REJECT_REASONS) == kql_reasons | {"unknown"}


def test_rotation_grace_and_rejections_each_log_one_line(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
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
