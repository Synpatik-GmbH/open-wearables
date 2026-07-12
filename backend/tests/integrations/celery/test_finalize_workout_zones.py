"""Unit tests for the ``finalize_workout_zones`` debounce task.

Fully mocked — the DB session, detail fetch, HR-zone compute, emit helper, Redis
client, and ``apply_async`` are all stubbed, so no Postgres/Redis/Docker or broker is
touched. The pure ``should_emit`` decision helper runs for real to exercise the
target / cap / stopped-growing branches end-to-end.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import app.integrations.celery.tasks.finalize_workout_zones_task as task_mod
from app.config import settings
from app.integrations.celery.tasks.finalize_workout_zones_task import finalize_workout_zones


def _session_local(db: MagicMock) -> MagicMock:
    """Return a fake ``SessionLocal`` whose ``with`` block yields ``db``."""

    @contextmanager
    def _ctx() -> Any:
        yield db

    mock = MagicMock()
    mock.side_effect = lambda: _ctx()
    return mock


def _record() -> MagicMock:
    record = MagicMock()
    record.id = uuid4()
    record.data_source_id = uuid4()
    return record


def _zones() -> dict[str, int | None]:
    return {f"hr_zone_{i}_min": i for i in range(1, 6)}


def _install(
    *,
    redis: MagicMock,
    db: MagicMock,
    completeness: float | None,
    detail: MagicMock | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Patch the task's collaborators; return (get_by_record_id, compute, emit) mocks."""
    detail = detail if detail is not None else MagicMock()
    get_detail = MagicMock(return_value=detail)
    compute = MagicMock(return_value=(_zones(), completeness))
    emit = MagicMock()

    patches = [
        patch.object(task_mod, "get_redis_client", return_value=redis),
        patch.object(task_mod, "SessionLocal", _session_local(db)),
        patch.object(task_mod.event_record_service.event_record_detail_repo, "get_by_record_id", get_detail),
        patch.object(task_mod.event_record_service, "_compute_workout_hr_zone_fields", compute),
        patch.object(task_mod.event_record_service, "_emit_workout_created_from_persisted", emit),
    ]
    for p in patches:
        p.start()
    return get_detail, compute, emit


class TestFinalizeWorkoutZones:
    def teardown_method(self) -> None:
        patch.stopall()

    def test_emits_and_marks_when_stable(self) -> None:
        """prev == cur (0.80) under target → settled → emit once, mark Redis, no reschedule."""
        workout_id = str(uuid4())
        record = _record()
        redis = MagicMock()
        redis.get.return_value = None
        redis.set.return_value = True  # won SETNX
        db = MagicMock()
        db.get.side_effect = [record, MagicMock()]  # EventRecord, DataSource
        _, compute, emit = _install(redis=redis, db=db, completeness=0.80)

        with patch.object(finalize_workout_zones, "apply_async") as apply_async:
            result = finalize_workout_zones(workout_id, prev_completeness=0.80, first_seen_iso=None)

        assert result == {"emitted": True, "completeness": 0.80}
        compute.assert_called_once()
        emit.assert_called_once()
        # emit receives (db, record, data_source, detail, zone_minutes, completeness)
        args = emit.call_args.args
        assert args[1] is record
        assert args[4] == _zones()
        assert args[5] == 0.80
        redis.set.assert_called_once_with(f"workout:{workout_id}:emitted", "1", nx=True, ex=3600)
        apply_async.assert_not_called()

    def test_reschedules_when_growing(self) -> None:
        """prev=None, cur=0.41 under target/cap → reschedule, carry completeness + first_seen."""
        workout_id = str(uuid4())
        redis = MagicMock()
        redis.get.return_value = None
        db = MagicMock()
        db.get.side_effect = [_record(), MagicMock()]
        _, _, emit = _install(redis=redis, db=db, completeness=0.41)

        with patch.object(finalize_workout_zones, "apply_async") as apply_async:
            result = finalize_workout_zones(workout_id, prev_completeness=None, first_seen_iso=None)

        assert result == {"emitted": False, "reason": "rescheduled", "completeness": 0.41}
        emit.assert_not_called()
        apply_async.assert_called_once()
        kwargs = apply_async.call_args.kwargs
        assert kwargs["args"] == [workout_id]
        assert kwargs["countdown"] == settings.workout_zone_debounce_seconds
        assert kwargs["kwargs"]["prev_completeness"] == 0.41
        # first_seen was stamped (None → now) and forwarded as an ISO string.
        datetime.fromisoformat(kwargs["kwargs"]["first_seen_iso"])

    def test_noop_when_already_emitted(self) -> None:
        """Redis emitted-marker present → early return, no compute, no emit."""
        workout_id = str(uuid4())
        redis = MagicMock()
        redis.get.return_value = "1"  # already emitted
        db = MagicMock()
        _, compute, emit = _install(redis=redis, db=db, completeness=1.0)

        with patch.object(finalize_workout_zones, "apply_async") as apply_async:
            result = finalize_workout_zones(workout_id)

        assert result == {"emitted": False, "reason": "already_emitted"}
        compute.assert_not_called()
        emit.assert_not_called()
        apply_async.assert_not_called()

    def test_noop_when_record_missing(self) -> None:
        """EventRecord fetch returns None → skip, no emit, no reschedule, no compute."""
        workout_id = str(uuid4())
        redis = MagicMock()
        redis.get.return_value = None
        db = MagicMock()
        db.get.return_value = None  # record missing
        _, compute, emit = _install(redis=redis, db=db, completeness=1.0)

        with patch.object(finalize_workout_zones, "apply_async") as apply_async:
            result = finalize_workout_zones(workout_id)

        assert result == {"emitted": False, "reason": "missing_record"}
        compute.assert_not_called()
        emit.assert_not_called()
        apply_async.assert_not_called()

    def test_emits_at_hard_cap(self) -> None:
        """Still growing (0.41 → 0.80) but past the hard cap → emit whatever we have."""
        workout_id = str(uuid4())
        redis = MagicMock()
        redis.get.return_value = None
        redis.set.return_value = True
        db = MagicMock()
        db.get.side_effect = [_record(), MagicMock()]
        _, _, emit = _install(redis=redis, db=db, completeness=0.80)

        first_seen = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
        with patch.object(finalize_workout_zones, "apply_async") as apply_async:
            result = finalize_workout_zones(workout_id, prev_completeness=0.41, first_seen_iso=first_seen)

        assert result == {"emitted": True, "completeness": 0.80}
        emit.assert_called_once()
        apply_async.assert_not_called()

    def test_emit_guard_prevents_double(self) -> None:
        """Lost the SETNX race → settled but another poll already emitted → do NOT emit."""
        workout_id = str(uuid4())
        redis = MagicMock()
        redis.get.return_value = None
        redis.set.return_value = None  # lost the race (nx failed)
        db = MagicMock()
        db.get.side_effect = [_record(), MagicMock()]
        _, _, emit = _install(redis=redis, db=db, completeness=0.80)

        with patch.object(finalize_workout_zones, "apply_async") as apply_async:
            result = finalize_workout_zones(workout_id, prev_completeness=0.80, first_seen_iso=None)

        assert result == {"emitted": True, "completeness": 0.80}
        emit.assert_not_called()
        apply_async.assert_not_called()
