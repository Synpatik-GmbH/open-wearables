"""2.96.6.3 (spec AC1): an SDK batch whose worker is killed in the middle of it is processed again
after the restart, and its data is stored.

Three real worker processes on the test Redis and Postgres. The first takes the batch and is
SIGKILLed while the import is in progress, before anything is written. The second starts with a
two-second visibility timeout in place of Kombu's hour, takes back the unacknowledged batch when
it starts consuming, and is killed the same way. The third takes it back again and stores it.
Without acks_late the first worker acknowledged the batch when its thread started it, so nothing
comes back and nothing is stored.

The workers commit for real, so the user is committed for real too, and deleting it (every foreign
key to it cascades) is checked against a row count of every table taken before.
"""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
import redis as redis_lib
from celery import Celery
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.database import BaseDbModel
from app.models import DataPointSeries, DataSource, EventRecord, User
from tests.tasks.sdk_upload_payload import night_payload

_TASK = "app.integrations.celery.tasks.process_sdk_upload_task.process_sdk_upload"
_BACKEND = Path(__file__).resolve().parents[2]
_VISIBILITY_TIMEOUT_SECONDS = 2
_WAIT_SECONDS = 90


def _row_counts(engine: Engine) -> dict[str, int]:
    with Session(bind=engine) as session:
        return {
            table.name: session.scalar(select(func.count()).select_from(table)) or 0
            for table in BaseDbModel.metadata.sorted_tables
        }


def _stored(engine: Engine, user_id: UUID) -> dict[str, int]:
    with Session(bind=engine) as session:
        sources = select(DataSource.id).where(DataSource.user_id == user_id)

        def count(model: Any, *where: Any) -> int:
            query = select(func.count()).select_from(model).where(model.data_source_id.in_(sources), *where)
            return session.scalar(query) or 0

        return {
            "series": count(DataPointSeries),
            "workouts": count(EventRecord, EventRecord.category == "workout"),
            "sleep": count(EventRecord, EventRecord.category == "sleep"),
        }


def _wait_for(condition: Callable[[], bool], worker: subprocess.Popen, what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert worker.poll() is None, f"worker exited ({worker.returncode}) before {what}"
        assert time.monotonic() < deadline, f"no {what} within {_WAIT_SECONDS}s"
        time.sleep(0.2)


@pytest.fixture
def committed_user(engine: Engine) -> Iterator[UUID]:
    before = _row_counts(engine)
    user_id = uuid4()
    with Session(bind=engine) as session:
        session.add(User(id=user_id, created_at=datetime.now(timezone.utc), email=f"kill-{user_id}@example.com"))
        session.commit()
    yield user_id
    with Session(bind=engine) as session:
        session.execute(delete(User).where(User.id == user_id))
        session.commit()
    assert _row_counts(engine) == before, "the workers left rows behind"


@pytest.fixture
def broker_url(_redis_url: str) -> str:
    return _redis_url


@pytest.fixture
def start_worker(_postgres_url: str, _redis_url: str, tmp_path: Path) -> Iterator[Callable[..., subprocess.Popen]]:
    db = urlparse(_postgres_url)
    redis = urlparse(_redis_url)
    base_env = {
        **os.environ,
        "DB_HOST": db.hostname or "localhost",
        "DB_PORT": str(db.port or 5432),
        "DB_NAME": db.path.lstrip("/"),
        "DB_USER": db.username or "",
        "DB_PASSWORD": db.password or "",
        "REDIS_HOST": redis.hostname or "localhost",
        "REDIS_PORT": str(redis.port or 6379),
        "REDIS_DB": redis.path.lstrip("/") or "0",
    }
    started: list[subprocess.Popen] = []

    def start(**env: str) -> subprocess.Popen:
        log = (tmp_path / f"worker-{len(started) + 1}.log").open("w")
        worker = subprocess.Popen(
            [sys.executable, "-m", "tests.tasks.sdk_upload_kill_worker"],
            cwd=_BACKEND,
            env={**base_env, **env},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        started.append(worker)
        return worker

    yield start
    # Only the workers this test started, by their own PIDs.
    for worker in started:
        if worker.poll() is None:
            worker.send_signal(signal.SIGTERM)
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=15)


def _kill_while_importing(worker: subprocess.Popen, hold_file: Path) -> float:
    _wait_for(hold_file.exists, worker, "import in progress")
    os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=15)
    return time.monotonic()


def _past_visibility_timeout(killed_at: float) -> None:
    # Kombu takes back a batch unacknowledged for longer than the visibility timeout.
    time.sleep(max(0.0, _VISIBILITY_TIMEOUT_SECONDS + 1 - (time.monotonic() - killed_at)))


def test_a_batch_whose_worker_is_killed_is_processed_again_and_stored(
    committed_user: UUID,
    start_worker: Callable[..., subprocess.Popen],
    engine: Engine,
    broker_url: str,
    tmp_path: Path,
) -> None:
    """Killed twice, then stored. The batch is published as attempt 2 (two in-task retries
    before), and both redeliveries count under that attempt: a redelivered message keeps its
    retries number, which is what the per-attempt count rests on (Codex P2, PR #44)."""
    batch_id = str(uuid4())
    count_key = f"sdk_upload:deliveries:{batch_id}:2"
    counts = redis_lib.from_url(broker_url, decode_responses=True)
    first_hold, second_hold = tmp_path / "first-import-started", tmp_path / "second-import-started"
    first = start_worker(SDK_UPLOAD_HOLD_FILE=str(first_hold))

    # Not the current app: the shared-task proxies other fixtures patch must keep resolving to
    # the application's own.
    publisher = Celery(broker=broker_url, set_as_current=False)
    publisher.send_task(
        _TASK,
        kwargs={
            "content": night_payload(),
            "content_type": "application/json",
            "user_id": str(committed_user),
            "provider": "apple",
            "batch_id": batch_id,
        },
        queue="sdk_sync",
        retries=2,
    )
    publisher.close()
    killed_at = _kill_while_importing(first, first_hold)
    assert counts.get(count_key) == "1"
    assert _stored(engine, committed_user) == {"series": 0, "workouts": 0, "sleep": 0}

    _past_visibility_timeout(killed_at)
    second = start_worker(
        SDK_UPLOAD_HOLD_FILE=str(second_hold), SDK_UPLOAD_VISIBILITY_TIMEOUT=str(_VISIBILITY_TIMEOUT_SECONDS)
    )
    killed_at = _kill_while_importing(second, second_hold)
    assert counts.get(count_key) == "2"
    assert _stored(engine, committed_user) == {"series": 0, "workouts": 0, "sleep": 0}

    _past_visibility_timeout(killed_at)
    third = start_worker(SDK_UPLOAD_VISIBILITY_TIMEOUT=str(_VISIBILITY_TIMEOUT_SECONDS))

    stored = {"series": 5, "workouts": 1, "sleep": 1}
    _wait_for(lambda: _stored(engine, committed_user) == stored, third, "redone batch stored")
    _wait_for(lambda: counts.exists(count_key) == 0, third, "count cleared")
