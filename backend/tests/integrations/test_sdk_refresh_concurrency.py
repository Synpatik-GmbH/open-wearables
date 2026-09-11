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
    assert isinstance(error, HTTPException), result_a
    assert error.status_code == 401, result_a


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
