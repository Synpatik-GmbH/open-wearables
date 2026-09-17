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


def wait_until_blocked_or_done(factory: sessionmaker, pid: int, done: threading.Event, timeout: float = 10.0) -> None:
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
