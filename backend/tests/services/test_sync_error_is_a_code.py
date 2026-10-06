"""A failed sync keeps an error code, never the error's text (fork, Notion 2.47.17.2).

An exception's text is not ours to bound. A database error quotes the statement's
parameters, which are the health values being saved and the user id. So nothing a sync
event leaves behind may carry that text: not the stored run, not the per-data-type
rows, not the Redis history, not the outgoing webhook, not the log line.
"""

import importlib
import json
import pkgutil
import subprocess
import sys
import time
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

import app
import app.integrations.celery.tasks.close_stale_sync_runs_task as sweep_task
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
            "UniqueViolation",
            "UnsupportedGranularityError",
            "WithingsTokenError_401",
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

    def test_a_class_this_process_merely_happens_to_have_loaded_is_not_a_code(self) -> None:
        """What is a code must not depend on which process asks: a worker stores it and the
        start-up script, which has loaded far less, reads it back."""

        class OnlyThisProcessKnowsMeError(Exception):
            pass

        assert error_code("OnlyThisProcessKnowsMeError") == UNCLASSIFIED
        assert error_code(OnlyThisProcessKnowsMeError(DB_ERROR_TEXT)) == UNCLASSIFIED

    def test_every_exception_the_backend_defines_is_a_code_in_a_process_that_loaded_nothing(self) -> None:
        """Asked of a fresh interpreter that has imported only the module under test, so a
        class counts because it is registered, not because something loaded it first."""
        for module in pkgutil.walk_packages(app.__path__, "app."):
            importlib.import_module(module.name)
        defined = sorted(
            {cls.__name__ for cls in _all_subclasses(BaseException) if cls.__module__.startswith("app.")},
        )
        assert "WithingsTokenError" in defined
        assert "UnsupportedGranularityError" in defined

        asked = [*defined, "WithingsTokenError_401", "UniqueViolation", "ConnectError", "IntegrityError"]
        script = (
            "import sys, json; from app.services.sync_error_code import error_code; "
            "print(json.dumps([error_code(name) for name in sys.argv[1:]]))"
        )
        answered = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script, *asked], capture_output=True, text=True, check=True
        ).stdout.splitlines()[-1]

        assert json.loads(answered) == asked

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


def _all_subclasses(cls: type) -> set[type]:
    found: set[type] = set()
    for subclass in type.__subclasses__(cls):
        if subclass not in found:
            found.add(subclass)
            found |= _all_subclasses(subclass)
    return found


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

    def test_what_the_caller_gets_back(self, outgoing: dict[str, MagicMock]) -> None:
        """The helpers return the event. It is the one that was stored and sent."""
        returned = sync_status_service.emit_sync_failed(
            uuid4(),
            "whoop",
            SyncSource.PULL,
            run_id="pull_returned",
            error=DB_ERROR_TEXT,
            metadata={"params": {"workouts": {"error": DB_ERROR_TEXT}}},
        )

        assert returned.error == UNCLASSIFIED
        assert returned.metadata == {"params": {"workouts": {"error": UNCLASSIFIED}}}
        assert sync_status_service.emit(_event(uuid4(), scope=SyncScope.LIVE)).error == UNCLASSIFIED

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
    def test_the_sweep_stops_looking_once_the_rollout_is_over(self, mock_task_session: MagicMock, db: Session) -> None:
        """An older worker can only be around while this image is being rolled out, so the
        sweep makes its pass for the first hours of its own process and then stops. The
        tables grow without bound and have no index for this, so it must not scan forever."""
        mock_task_session.return_value.__enter__.return_value = db
        user = UserFactory()
        now = datetime.now(timezone.utc)
        run = _stored_as_an_older_image_left_it(db, user.id, updated_at=now)

        with patch.object(
            sweep_task, "_PROCESS_STARTED_AT", now - sweep_task.ERROR_CLEANUP_PERIOD - timedelta(minutes=1)
        ):
            late = close_stale_sync_runs()
        db.expire_all()
        untouched = run.error

        with patch.object(
            sweep_task, "_PROCESS_STARTED_AT", now - sweep_task.ERROR_CLEANUP_PERIOD + timedelta(minutes=1)
        ):
            in_time = close_stale_sync_runs()
        db.expire_all()

        # None, not zero: the pass did not run, which is not the same as finding nothing.
        assert late["errors_reduced"] is None
        assert untouched == DB_ERROR_TEXT
        assert in_time["errors_reduced"] == {"runs": 1, "data_types": 1}
        assert run.error == UNCLASSIFIED

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


class TestThePhoneImportAnswersWithACode:
    """The import's answer becomes the upload task's result, which the worker logs and the
    result backend keeps, and it is logged once more on the way."""

    def test_a_failed_import_names_the_error_class_not_its_text(self, db: Session) -> None:
        from app.services.sdk.import_service import import_service

        user = UserFactory()

        with patch.object(import_service, "_parse_json_content", side_effect=RuntimeError(DB_ERROR_TEXT)):
            response = import_service.import_data_from_request(db, "{}", "application/json", str(user.id))

        assert response.status_code == 400
        assert response.response == "Import failed: RuntimeError"


class TestTheWebhookIsDescribedAsItIs:
    def test_sync_failed_promises_a_code_not_a_message(self) -> None:
        from app.schemas.webhooks.event_types import EVENT_TYPE_DESCRIPTIONS, WebhookEventType

        description = EVENT_TYPE_DESCRIPTIONS[WebhookEventType.SYNC_FAILED]

        assert "error code" in description
        assert "includes error message" not in description
        assert UNCLASSIFIED in description


class TestOnlyRowsThatCanHoldAnErrorAreRead:
    """The pass rewrites in Python, so the database hands it only rows with an error
    somewhere: in the column, or under an ``error`` key at any depth of the metadata."""

    def test_runs(self, db: Session) -> None:
        user = UserFactory()
        now = datetime.now(timezone.utc)

        def stored(error: str | None, meta: dict[str, Any] | None, updated_at: datetime = now) -> UUID:
            run_key = f"pull_{uuid4().hex[:16]}"
            return sync_run_repository.upsert_run(
                db,
                SyncRunWrite(
                    run_key=run_key,
                    user_id=user.id,
                    provider="whoop",
                    source=SyncSource.BACKFILL,
                    scope=SyncScope.HISTORICAL,
                    status=SyncStatus.SUCCESS,
                    started_at=updated_at,
                    error=error,
                    meta=meta,
                    updated_at=updated_at,
                ),
            )

        in_column = stored("KeyError", None)
        nested = stored(None, {"params": {"workouts": {"success": False, "error": "KeyError"}}})
        in_a_list = stored(None, {"results": [{"error": "KeyError"}]})
        stored(None, {"params": {"workouts": {"success": True, "note": "no error here"}}})
        stored(None, None)
        old = stored("KeyError", None, updated_at=now - timedelta(days=3))

        everything = {run.id for run in sync_run_repository.runs_with_an_error(db)}
        recent = {run.id for run in sync_run_repository.runs_with_an_error(db, since=now - timedelta(days=1))}

        assert everything == {in_column, nested, in_a_list, old}
        assert recent == {in_column, nested, in_a_list}
