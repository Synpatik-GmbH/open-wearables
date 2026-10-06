"""A failed sync keeps an error code, never the error's text (fork, Notion 2.47.17.2).

An exception's text is not ours to bound. A database error quotes the statement's
parameters, which are the health values being saved and the user id. So nothing a sync
event leaves behind may carry that text: not the stored run, not the per-data-type
rows, not the Redis history, not the outgoing webhook, not the log line.
"""

import json
import time
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

import app.services.sync_status_service as sync_status_service
from app.integrations.celery.tasks.close_stale_sync_runs_task import close_stale_sync_runs
from app.models import SyncRun, SyncRunDataType
from app.repositories.sync_run_repository import sync_run_repository
from app.schemas.sync_status import (
    DataTypeKind,
    DataTypeOutcome,
    SyncRunWrite,
    SyncScope,
    SyncSource,
    SyncStage,
    SyncStatus,
    SyncStatusEvent,
)
from app.services.sync_error_code import (
    ALL_SUBTASKS_FAILED,
    SDK_IMPORT_FAILED,
    UNCLASSIFIED,
    UNKNOWN,
    error_code,
    without_error_text,
)
from tests.factories import UserFactory

# What a SQLAlchemy error's text looks like: the statement and its parameters.
HEALTH_VALUE = "187.5"
LEAKED_USER = "5f0c1c1e-7d1b-4a55-9d0e-2f6b6f1c9a11"
DB_ERROR_TEXT = (
    "(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint\n"
    "[SQL: INSERT INTO data_point_series (user_id, value) VALUES (%(user_id)s, %(value)s)]\n"
    f"[parameters: {{'user_id': '{LEAKED_USER}', 'value': {HEALTH_VALUE}}}]"
)


class TestErrorCode:
    @pytest.mark.parametrize(
        "code",
        [
            "IntegrityError",
            "KeyError",
            "HTTPException_404",
            "HTTPException_599",
            ALL_SUBTASKS_FAILED,
            SDK_IMPORT_FAILED,
            UNKNOWN,
            UNCLASSIFIED,
        ],
    )
    def test_an_exception_class_name_or_a_fixed_word_is_kept(self, code: str) -> None:
        assert error_code(code) == code

    def test_a_class_defined_after_the_first_lookup_is_known(self) -> None:
        assert error_code("LateArrivingSyncError") == UNCLASSIFIED

        class LateArrivingSyncError(Exception):
            pass

        assert error_code("LateArrivingSyncError") == "LateArrivingSyncError"
        assert error_code(LateArrivingSyncError(DB_ERROR_TEXT)) == "LateArrivingSyncError"

    @pytest.mark.parametrize(
        "text",
        [
            DB_ERROR_TEXT,
            "Whoop authorization expired. Please re-authorize.",
            LEAKED_USER,
            LEAKED_USER.replace("-", ""),
            HEALTH_VALUE,
            "72",
            "bpm_18750",
            "user@example.com",
            "'heart_rate'",
            "a" * 64,
            "",
            # One word, shaped like a code, but nobody's code: a name, a phone's own
            # error code, a class that does not exist.
            "Alice",
            "heartrate",
            "HKError_5",
            "OAuth2Error",
            # A status is kept only behind an exception class, and only as a status.
            "IntegrityError_40",
            "IntegrityError_600",
            "IntegrityError_4040",
            "all_subtasks_failed_404",
            "_404",
            "IntegrityError_",
            " IntegrityError",
            "IntegrityError\n",
        ],
    )
    def test_anything_else_is_unclassified(self, text: str) -> None:
        assert error_code(text) == UNCLASSIFIED

    def test_no_error_stays_no_error(self) -> None:
        assert error_code(None) is None

    def test_an_exception_gives_its_class_name_not_its_text(self) -> None:
        assert error_code(ValueError(DB_ERROR_TEXT)) == "ValueError"

    def test_an_http_error_keeps_its_status(self) -> None:
        assert error_code(HTTPException(status_code=401, detail=DB_ERROR_TEXT)) == "HTTPException_401"

    def test_a_status_that_is_not_a_status_is_left_out(self) -> None:
        exc = HTTPException(status_code=401, detail="x")
        exc.status_code = LEAKED_USER  # type: ignore[assignment]

        assert error_code(exc) == "HTTPException"


class TestWithoutErrorText:
    def test_every_depth_and_inside_lists(self) -> None:
        cleaned = without_error_text(
            {
                "error": DB_ERROR_TEXT,
                "results": [{"status": "error", "error": DB_ERROR_TEXT}, {"status": "ok", "error": "IntegrityError"}],
                "params": {"workouts": {"nested": {"error": DB_ERROR_TEXT}}},
                "error_count": 2,
            },
        )

        assert cleaned == {
            "error": UNCLASSIFIED,
            "results": [{"status": "error", "error": UNCLASSIFIED}, {"status": "ok", "error": "IntegrityError"}],
            "params": {"workouts": {"nested": {"error": UNCLASSIFIED}}},
            "error_count": 2,
        }

    @pytest.mark.parametrize("value", [None, True, False])
    def test_no_error_and_flags_are_left_as_they_are(self, value: object) -> None:
        assert without_error_text({"error": value}) == {"error": value}

    @pytest.mark.parametrize("value", [{"detail": DB_ERROR_TEXT}, [DB_ERROR_TEXT], 187.5])
    def test_an_error_that_is_not_a_string_is_dropped_too(self, value: object) -> None:
        assert without_error_text({"error": value}) == {"error": UNCLASSIFIED}


def _event(user_id: UUID, **overrides: Any) -> SyncStatusEvent:
    defaults: dict[str, Any] = {
        "run_id": f"pull_{uuid4().hex[:16]}",
        "user_id": user_id,
        "provider": "whoop",
        "source": SyncSource.BACKFILL,
        "scope": SyncScope.HISTORICAL,
        "stage": SyncStage.FAILED,
        "status": SyncStatus.FAILED,
        "started_at": datetime.now(timezone.utc),
        "error": DB_ERROR_TEXT,
        "metadata": {
            "is_historical": True,
            "inserted": 3,
            "params": {
                "workouts": {"success": False, "error": DB_ERROR_TEXT},
                "data_247": {"success": True, "sleep_sessions_synced": 2, "error": None},
            },
        },
    }
    return SyncStatusEvent(**{**defaults, **overrides})


def _assert_clean(text: str) -> None:
    assert LEAKED_USER not in text
    assert HEALTH_VALUE not in text
    assert "INSERT INTO" not in text


@pytest.fixture
def outgoing() -> Generator[dict[str, MagicMock], None, None]:
    with (
        patch("app.services.outgoing_webhooks.events.on_sync_started") as started,
        patch("app.services.outgoing_webhooks.events.on_sync_completed") as completed,
        patch("app.services.outgoing_webhooks.events.on_sync_failed") as failed,
    ):
        yield {"started": started, "completed": completed, "failed": failed}


def _wait_for(mock: MagicMock) -> None:
    deadline = time.monotonic() + 3
    while not mock.called and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mock.called


# One case per stage that is stored, each carrying the error text where that stage can.
STORED_STAGES = [
    (SyncStage.FAILED, SyncStatus.FAILED),
    (SyncStage.COMPLETED, SyncStatus.PARTIAL),
    (SyncStage.CANCELLED, SyncStatus.CANCELLED),
]


class TestEmitLeavesNoErrorText:
    @pytest.mark.parametrize(("stage", "status"), STORED_STAGES)
    @patch("app.services.sync_status_service.SessionLocal")
    def test_the_stored_run(
        self,
        mock_session_local: MagicMock,
        db: Session,
        outgoing: dict[str, MagicMock],
        stage: SyncStage,
        status: SyncStatus,
    ) -> None:
        mock_session_local.return_value.__enter__.return_value = db
        user = UserFactory()
        event = _event(user.id, stage=stage, status=status)

        sync_status_service.emit(event)

        run = db.query(SyncRun).filter(SyncRun.run_key == event.run_id).one()
        assert run.error == UNCLASSIFIED
        assert run.meta is not None
        assert run.meta["params"]["workouts"] == {"success": False, "error": UNCLASSIFIED}
        # What is not error text is left alone.
        assert run.meta["params"]["data_247"] == {"success": True, "sleep_sessions_synced": 2, "error": None}
        assert run.meta["is_historical"] is True
        _assert_clean(json.dumps(run.meta))

    @patch("app.services.sync_status_service.SessionLocal")
    def test_a_code_from_the_caller_is_what_is_stored(
        self, mock_session_local: MagicMock, db: Session, outgoing: dict[str, MagicMock]
    ) -> None:
        mock_session_local.return_value.__enter__.return_value = db
        user = UserFactory()
        event = _event(user.id, error=error_code(HTTPException(status_code=401, detail=DB_ERROR_TEXT)))

        sync_status_service.emit(event)

        run = db.query(SyncRun).filter(SyncRun.run_key == event.run_id).one()
        assert run.error == "HTTPException_401"

    def test_the_redis_history(self, outgoing: dict[str, MagicMock]) -> None:
        user_id = uuid4()
        event = _event(user_id, scope=SyncScope.LIVE)

        sync_status_service.emit(event)

        recent = sync_status_service.get_recent_events(user_id)
        assert [e.error for e in recent] == [UNCLASSIFIED]
        _assert_clean(recent[0].model_dump_json())
        summaries = sync_status_service.get_run_summaries(user_id)
        assert [s.error for s in summaries] == [UNCLASSIFIED]

    def test_the_outgoing_webhook(self, outgoing: dict[str, MagicMock]) -> None:
        event = _event(uuid4(), scope=SyncScope.LIVE)

        sync_status_service.emit(event)

        _wait_for(outgoing["failed"])
        kwargs = outgoing["failed"].call_args.kwargs
        assert kwargs["error"] == UNCLASSIFIED
        _assert_clean(json.dumps(kwargs, default=str))

    def test_the_outgoing_webhook_of_a_run_that_completed_with_errors(self, outgoing: dict[str, MagicMock]) -> None:
        event = _event(uuid4(), scope=SyncScope.LIVE, stage=SyncStage.COMPLETED, status=SyncStatus.PARTIAL)

        sync_status_service.emit(event)

        _wait_for(outgoing["completed"])
        _assert_clean(json.dumps(outgoing["completed"].call_args.kwargs, default=str))

    def test_the_log_line(
        self, outgoing: dict[str, MagicMock], capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        event = _event(uuid4(), scope=SyncScope.LIVE)

        sync_status_service.emit(event)

        out, err = capfd.readouterr()
        logged = out + err + caplog.text
        assert '"action": "sync_status"' in logged
        assert f'"error": "{UNCLASSIFIED}"' in logged
        _assert_clean(logged)


class TestDataTypeRowsHoldNoErrorText:
    @patch("app.services.sync_status_service.SessionLocal")
    def test_error_and_error_code_are_codes(self, mock_session_local: MagicMock, db: Session) -> None:
        mock_session_local.return_value.__enter__.return_value = db
        user = UserFactory()
        event = _event(user.id, stage=SyncStage.STARTED, status=SyncStatus.IN_PROGRESS, error=None, metadata={})
        sync_status_service.try_persist_run(event)

        sync_status_service.try_record_data_types(
            event.run_id,
            [
                DataTypeOutcome(
                    data_type="workouts",
                    kind=DataTypeKind.TASK,
                    status=SyncStatus.FAILED,
                    error=DB_ERROR_TEXT,
                    error_code=DB_ERROR_TEXT,
                ),
                DataTypeOutcome(
                    data_type="steps",
                    kind=DataTypeKind.SERIES,
                    status=SyncStatus.FAILED,
                    error="IntegrityError",
                    error_code="KeyError",
                ),
                # What a phone reports as its own error code is not ours to trust either.
                DataTypeOutcome(
                    data_type="sleep",
                    kind=DataTypeKind.SERIES,
                    status=SyncStatus.FAILED,
                    error_code="HKError_5",
                ),
            ],
            scope=SyncScope.HISTORICAL,
        )

        rows = {row.data_type: row for row in db.query(SyncRunDataType).all()}
        assert (rows["workouts"].error, rows["workouts"].error_code) == (UNCLASSIFIED, UNCLASSIFIED)
        assert (rows["steps"].error, rows["steps"].error_code) == ("IntegrityError", "KeyError")
        assert (rows["sleep"].error, rows["sleep"].error_code) == (None, UNCLASSIFIED)

    @patch("app.services.sync_status_service.SessionLocal")
    def test_a_storage_failure_logs_its_type_not_its_text(
        self,
        mock_session_local: MagicMock,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_session_local.return_value.__enter__.side_effect = RuntimeError(DB_ERROR_TEXT)

        sync_status_service.try_record_data_types(
            "pull_x",
            [DataTypeOutcome(data_type="workouts", kind=DataTypeKind.TASK, status=SyncStatus.FAILED)],
            scope=SyncScope.HISTORICAL,
        )

        out, err = capfd.readouterr()
        logged = out + err + caplog.text
        assert '"action": "sync_run_data_types_failed"' in logged
        assert '"error": "RuntimeError"' in logged
        _assert_clean(logged)


def _stored_as_an_older_image_left_it(
    db: Session, user_id: UUID, *, updated_at: datetime, error: str | None = DB_ERROR_TEXT
) -> SyncRun:
    """Written through the repository, which does no cleaning."""
    run_key = f"pull_{uuid4().hex[:16]}"
    sync_run_repository.upsert_run(
        db,
        SyncRunWrite(
            run_key=run_key,
            user_id=user_id,
            provider="whoop",
            source=SyncSource.BACKFILL,
            scope=SyncScope.HISTORICAL,
            status=SyncStatus.FAILED,
            started_at=updated_at,
            ended_at=updated_at,
            error=error,
            meta={"params": {"workouts": {"error": DB_ERROR_TEXT}}},
            updated_at=updated_at,
        ),
    )
    run = sync_run_repository.get_by_run_key(db, run_key)
    assert run is not None
    sync_run_repository.upsert_data_types(
        db,
        run_id=run.id,
        outcomes=[
            DataTypeOutcome(data_type="workouts", kind=DataTypeKind.TASK, status=SyncStatus.FAILED, error=DB_ERROR_TEXT)
        ],
        updated_at=updated_at,
    )
    return run


class TestTheSweepReducesWhatAnOlderWorkerWrote:
    """During a rolling deploy a worker on the older image can store text after the API's
    start-up pass has run. The periodic sweep is the pass that comes after it."""

    @patch("app.integrations.celery.tasks.close_stale_sync_runs_task.SessionLocal")
    def test_a_row_written_since_is_reduced_and_counted(
        self,
        mock_task_session: MagicMock,
        db: Session,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_task_session.return_value.__enter__.return_value = db
        user = UserFactory()
        run = _stored_as_an_older_image_left_it(db, user.id, updated_at=datetime.now(timezone.utc))

        with patch.object(db, "commit", wraps=db.commit) as commit:
            result = close_stale_sync_runs()

        assert result["errors_reduced"] == {"runs": 1, "data_types": 1}
        commit.assert_called_once()
        db.expire_all()
        assert run.error == UNCLASSIFIED
        assert run.meta == {"params": {"workouts": {"error": UNCLASSIFIED}}}
        assert [row.error for row in db.query(SyncRunDataType).all()] == [UNCLASSIFIED]
        out, err = capfd.readouterr()
        logged = out + err + caplog.text
        assert '"action": "sync_run_errors_reduced"' in logged
        assert '"runs": 1' in logged
        _assert_clean(logged)

    @patch("app.integrations.celery.tasks.close_stale_sync_runs_task.SessionLocal")
    def test_a_run_that_completed_with_errors_is_reduced_too(self, mock_task_session: MagicMock, db: Session) -> None:
        """It has no error of its own: the text is only inside its metadata."""
        mock_task_session.return_value.__enter__.return_value = db
        user = UserFactory()
        run = _stored_as_an_older_image_left_it(db, user.id, updated_at=datetime.now(timezone.utc), error=None)

        result = close_stale_sync_runs()

        assert result["errors_reduced"] == {"runs": 1, "data_types": 1}
        db.expire_all()
        assert run.error is None
        assert run.meta == {"params": {"workouts": {"error": UNCLASSIFIED}}}

    @patch("app.integrations.celery.tasks.close_stale_sync_runs_task.SessionLocal")
    def test_nothing_to_reduce_is_reported_as_zero(self, mock_task_session: MagicMock, db: Session) -> None:
        mock_task_session.return_value.__enter__.return_value = db

        assert close_stale_sync_runs()["errors_reduced"] == {"runs": 0, "data_types": 0}

    @patch("app.integrations.celery.tasks.close_stale_sync_runs_task.SessionLocal")
    def test_the_sweep_reads_the_last_day_only(self, mock_task_session: MagicMock, db: Session) -> None:
        """Older rows are the start-up script's: it reads every row. The sweep runs every
        half hour, so it reads only what can have been written since."""
        mock_task_session.return_value.__enter__.return_value = db
        user = UserFactory()
        now = datetime.now(timezone.utc)
        inside = _stored_as_an_older_image_left_it(db, user.id, updated_at=now - timedelta(hours=23))
        outside = _stored_as_an_older_image_left_it(db, user.id, updated_at=now - timedelta(hours=25))

        result = close_stale_sync_runs()

        assert result["errors_reduced"] == {"runs": 1, "data_types": 1}
        db.expire_all()
        assert inside.error == UNCLASSIFIED
        assert outside.error == DB_ERROR_TEXT

    @pytest.mark.parametrize("redis_readable", [True, False])
    @patch("app.integrations.celery.tasks.close_stale_sync_runs_task.last_event_at")
    @patch("app.integrations.celery.tasks.close_stale_sync_runs_task.SessionLocal")
    @patch("app.services.sync_status_service.SessionLocal")
    def test_the_count_is_reported_on_every_way_out_of_the_sweep(
        self,
        mock_emit_session: MagicMock,
        mock_task_session: MagicMock,
        mock_last_event: MagicMock,
        db: Session,
        redis_readable: bool,
    ) -> None:
        mock_emit_session.return_value.__enter__.return_value = db
        mock_task_session.return_value.__enter__.return_value = db
        mock_last_event.return_value = {} if redis_readable else None
        user = UserFactory()
        now = datetime.now(timezone.utc)
        _stored_as_an_older_image_left_it(db, user.id, updated_at=now)
        old = now - timedelta(hours=48)
        sync_status_service.try_persist_run(
            _event(
                user.id,
                run_id="pull_stale",
                stage=SyncStage.STARTED,
                status=SyncStatus.IN_PROGRESS,
                error=None,
                metadata={},
                started_at=old,
                timestamp=old,
            ),
        )

        result = close_stale_sync_runs()

        # Each case leaves by its own return: the stale run closed, or the sweep skipped.
        if redis_readable:
            assert result["run_keys"] == ["pull_stale"]
        else:
            assert result["skipped"] is True
        assert result["errors_reduced"] == {"runs": 1, "data_types": 1}
