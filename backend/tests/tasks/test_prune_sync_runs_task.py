"""
FORK (data protection, Notion 2.47.17.1): stored sync runs are kept for a period, not for
as long as the account lives.

A run is of no use once its sync is done. The daily task removes every run stored longer
ago than ``sync_run_retention_days``, with its per-data-type rows, and reports how many it
removed and how old the oldest run it left is.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from celery.schedules import crontab
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.celery.core import create_celery
from app.integrations.celery.tasks import prune_sync_runs_task as task_module
from app.integrations.celery.tasks.prune_sync_runs_task import prune_old_sync_runs
from app.models import SyncRun, SyncRunDataType
from app.schemas.sync_status import DataTypeKind, SyncScope, SyncSource, SyncStatus
from tests.factories import UserFactory

PERIOD = timedelta(days=90)
# The task reads the clock itself, a moment after the test does.
MARGIN = timedelta(minutes=5)


def _store(db: Session, user_id: UUID, *, stored: timedelta, **overrides: Any) -> SyncRun:
    """A run stored ``stored`` ago."""
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "id": uuid4(),
        "run_key": f"pull_{uuid4().hex[:16]}",
        "user_id": user_id,
        "provider": "oura",
        "source": SyncSource.PULL,
        "scope": SyncScope.HISTORICAL,
        "status": SyncStatus.SUCCESS,
        "started_at": now - stored,
        "updated_at": now - stored,
        "created_at": now - stored,
        "items_inserted": 0,
        "items_updated": 0,
    }
    run = SyncRun(**{**values, **overrides})
    db.add(run)
    db.commit()
    return run


def _store_data_type(db: Session, run: SyncRun, data_type: str = "workouts") -> None:
    db.add(
        SyncRunDataType(
            run_id=run.id,
            data_type=data_type,
            kind=DataTypeKind.TASK,
            status=SyncStatus.SUCCESS,
            attempt=1,
            items_inserted=0,
            items_updated=0,
            updated_at=run.updated_at,
        )
    )
    db.commit()


def _stored_keys(db: Session) -> set[str]:
    db.expire_all()
    return {key for (key,) in db.query(SyncRun.run_key).all()}


@pytest.fixture
def task_db(db: Session) -> Any:
    with patch.object(task_module, "SessionLocal") as session_local:
        session_local.return_value.__enter__.return_value = db
        yield db


class TestWhichRunsAreRemoved:
    def test_a_run_stored_before_the_period_goes_and_one_inside_it_stays(self, task_db: Session) -> None:
        user = UserFactory()
        _store(task_db, user.id, stored=PERIOD + MARGIN, run_key="pull_too_old")
        _store(task_db, user.id, stored=PERIOD - MARGIN, run_key="pull_still_inside")
        _store(task_db, user.id, stored=timedelta(hours=1), run_key="pull_recent")

        result = prune_old_sync_runs()

        assert _stored_keys(task_db) == {"pull_still_inside", "pull_recent"}
        assert result["removed_count"] == 1

    def test_runs_of_every_user_are_covered(self, task_db: Session) -> None:
        first, second = UserFactory(), UserFactory()
        _store(task_db, first.id, stored=PERIOD + MARGIN)
        _store(task_db, second.id, stored=PERIOD + MARGIN)
        kept = _store(task_db, second.id, stored=timedelta(days=1))

        result = prune_old_sync_runs()

        assert _stored_keys(task_db) == {kept.run_key}
        assert result["removed_count"] == 2

    @pytest.mark.parametrize("status", list(SyncStatus))
    def test_an_old_run_goes_whatever_its_status(self, task_db: Session, status: SyncStatus) -> None:
        """A run left in progress for the whole period is as finished as any other."""
        user = UserFactory()
        _store(task_db, user.id, stored=PERIOD + MARGIN, status=status)

        result = prune_old_sync_runs()

        assert _stored_keys(task_db) == set()
        assert result["removed_count"] == 1

    @pytest.mark.parametrize("scope", list(SyncScope))
    @pytest.mark.parametrize("source", list(SyncSource))
    def test_an_old_run_goes_whatever_its_scope_and_source(
        self, task_db: Session, scope: SyncScope, source: SyncSource
    ) -> None:
        user = UserFactory()
        _store(task_db, user.id, stored=PERIOD + MARGIN, scope=scope, source=source)

        prune_old_sync_runs()

        assert _stored_keys(task_db) == set()

    def test_the_age_is_counted_from_when_the_run_was_stored(self, task_db: Session) -> None:
        """started_at and updated_at come from the event, which for an SDK run is the
        device's clock. Only created_at is this server's."""
        user = UserFactory()
        now = datetime.now(timezone.utc)
        _store(task_db, user.id, stored=PERIOD + MARGIN, started_at=now, updated_at=now, run_key="pull_stored_long_ago")
        _store(
            task_db,
            user.id,
            stored=timedelta(hours=1),
            started_at=now - PERIOD - MARGIN,
            updated_at=now - PERIOD - MARGIN,
            run_key="pull_stored_today",
        )

        prune_old_sync_runs()

        assert _stored_keys(task_db) == {"pull_stored_today"}

    def test_the_per_data_type_rows_go_with_their_run_and_no_others(self, task_db: Session) -> None:
        user = UserFactory()
        too_old = _store(task_db, user.id, stored=PERIOD + MARGIN)
        kept = _store(task_db, user.id, stored=timedelta(days=1))
        too_old_id, kept_id = too_old.id, kept.id
        _store_data_type(task_db, too_old, "workouts")
        _store_data_type(task_db, too_old, "sleep")
        _store_data_type(task_db, kept, "workouts")

        prune_old_sync_runs()

        task_db.expire_all()
        remaining = {run_id for (run_id,) in task_db.query(SyncRunDataType.run_id).all()}
        assert remaining == {kept_id}
        assert too_old_id not in remaining


class TestThePeriod:
    def test_it_is_ninety_days_unless_set(self) -> None:
        assert type(settings).model_fields["sync_run_retention_days"].default == 90

    def test_the_task_uses_the_setting(self, task_db: Session) -> None:
        user = UserFactory()
        _store(task_db, user.id, stored=timedelta(days=10))
        kept = _store(task_db, user.id, stored=timedelta(days=5))

        with patch.object(settings, "sync_run_retention_days", 7):
            result = prune_old_sync_runs()

        assert _stored_keys(task_db) == {kept.run_key}
        assert result["retention_days"] == 7

    def test_a_period_of_no_days_is_refused(self) -> None:
        """Zero would remove a run while its sync is still going."""
        with pytest.raises(ValidationError, match="sync_run_retention_days"):
            type(settings)(sync_run_retention_days=0)


class TestWhatTheRunReports:
    def test_the_count_removed_and_the_age_of_the_oldest_run_left(self, task_db: Session) -> None:
        user = UserFactory()
        _store(task_db, user.id, stored=PERIOD + MARGIN)
        _store(task_db, user.id, stored=PERIOD + timedelta(days=30))
        _store(task_db, user.id, stored=timedelta(days=40))
        _store(task_db, user.id, stored=timedelta(days=2))

        result = prune_old_sync_runs()

        assert result["removed_count"] == 2
        assert result["retention_days"] == 90
        assert result["oldest_remaining_age_days"] == pytest.approx(40, abs=0.1)

    def test_the_age_is_none_when_no_run_is_left(self, task_db: Session) -> None:
        user = UserFactory()
        _store(task_db, user.id, stored=PERIOD + MARGIN)

        result = prune_old_sync_runs()

        assert result == {"removed_count": 1, "retention_days": 90, "oldest_remaining_age_days": None}

    def test_nothing_to_remove_is_reported_as_zero(self, task_db: Session) -> None:
        user = UserFactory()
        _store(task_db, user.id, stored=timedelta(days=3))

        result = prune_old_sync_runs()

        assert result["removed_count"] == 0
        assert result["oldest_remaining_age_days"] == pytest.approx(3, abs=0.1)

    def test_every_run_writes_one_log_line_with_both_numbers(self, task_db: Session) -> None:
        """Also when nothing was removed: a day without the line is a day the task did not run."""
        user = UserFactory()
        _store(task_db, user.id, stored=timedelta(days=3))

        with patch.object(task_module, "log_structured") as log:
            prune_old_sync_runs()

        log.assert_called_once()
        assert log.call_args.kwargs["action"] == "sync_run_prune_complete"
        assert log.call_args.kwargs["removed_count"] == 0
        assert log.call_args.kwargs["retention_days"] == 90
        assert log.call_args.kwargs["oldest_remaining_age_days"] == pytest.approx(3, abs=0.1)

    def test_the_log_line_carries_no_user_or_run(self, task_db: Session) -> None:
        user = UserFactory()
        user_id = str(user.id)
        _store(task_db, user.id, stored=PERIOD + MARGIN, run_key="pull_removed")

        with patch.object(task_module, "log_structured") as log:
            prune_old_sync_runs()

        logged = repr(log.call_args)
        assert "removed_count" in logged
        assert user_id not in logged
        assert "pull_removed" not in logged


class TestTheSchedule:
    def test_beat_runs_the_task_once_a_day(self) -> None:
        schedule = create_celery().conf.beat_schedule

        entry = schedule["prune-old-sync-runs"]

        assert entry["task"] == prune_old_sync_runs.name
        assert isinstance(entry["schedule"], crontab)
        assert entry["schedule"].hour == {3}
        assert entry["schedule"].minute == {30}
        assert entry["schedule"].day_of_week == set(range(7))
        assert entry["schedule"].day_of_month == set(range(1, 32))

    def test_the_task_is_one_a_worker_loads(self) -> None:
        import app.integrations.celery.tasks as tasks

        assert tasks.prune_old_sync_runs is prune_old_sync_runs
        assert "prune_old_sync_runs" in tasks.__all__


class TestAFailure:
    def test_a_database_error_is_raised_not_swallowed(self) -> None:
        """So the run shows as failed, and not as a day with nothing to remove."""
        session = MagicMock()
        session.scalars.side_effect = RuntimeError("connection lost")
        with patch.object(task_module, "SessionLocal") as session_local:
            session_local.return_value.__enter__.return_value = session
            with pytest.raises(RuntimeError):
                prune_old_sync_runs()
