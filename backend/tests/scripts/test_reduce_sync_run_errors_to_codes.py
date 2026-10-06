"""Tests for the data migration that reduces stored sync errors to codes (Notion 2.47.17.2).

Runs stored before the fork kept codes only can hold an error's raw text, which for a
database error quotes health values and the user id. The script rewrites those rows.
Rows are written here through the repository, which does no cleaning, so they are the
rows an older image left behind. See scripts/data_migrations/reduce_sync_run_errors_to_codes.py.
"""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import SyncRun, SyncRunDataType
from app.repositories.sync_run_repository import sync_run_repository
from app.schemas.sync_status import (
    DataTypeKind,
    DataTypeOutcome,
    SyncRunWrite,
    SyncScope,
    SyncSource,
    SyncStatus,
)
from tests.factories import UserFactory

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "reduce_sync_run_errors_to_codes.py"
)

TEXT = "[SQL: INSERT INTO data_point_series ...] [parameters: {'user_id': '5f0c1c1e', 'value': 187.5}]"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reduce_sync_run_errors_to_codes", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reduce_sync_run_errors = _load_module().reduce_sync_run_errors


def _run(db: Session, user_id: UUID, *, error: str | None, meta: dict[str, Any] | None) -> SyncRun:
    now = datetime.now(timezone.utc)
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
            started_at=now,
            ended_at=now,
            error=error,
            meta=meta,
            updated_at=now,
        ),
    )
    run = sync_run_repository.get_by_run_key(db, run_key)
    assert run is not None
    return run


def _data_type(db: Session, run: SyncRun, data_type: str, *, error: str | None, error_code: str | None) -> None:
    sync_run_repository.upsert_data_types(
        db,
        run_id=run.id,
        outcomes=[
            DataTypeOutcome(
                data_type=data_type,
                kind=DataTypeKind.TASK,
                status=SyncStatus.FAILED,
                error=error,
                error_code=error_code,
            ),
        ],
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def stored(db: Session) -> dict[str, SyncRun]:
    user = UserFactory()
    text_error = _run(db, user.id, error=TEXT, meta={"is_historical": True})
    text_meta = _run(db, user.id, error="all_subtasks_failed", meta={"params": {"workouts": {"error": TEXT}}})
    clean = _run(db, user.id, error="IntegrityError", meta={"params": {"workouts": {"error": "KeyError"}}})
    no_error = _run(db, user.id, error=None, meta=None)
    # A run that completed with errors has no error of its own, only the nested one.
    meta_only = _run(db, user.id, error=None, meta={"params": {"data_247": {"error": TEXT}}})
    _data_type(db, text_error, "workouts", error=TEXT, error_code=None)
    # error_code is varchar(64), so the text it can hold is short.
    _data_type(db, text_error, "sleep", error=None, error_code="value 187.5 rejected")
    _data_type(db, clean, "workouts", error="KeyError", error_code="HKError_5")
    _data_type(db, clean, "sleep", error=None, error_code=None)
    db.flush()
    return {
        "text_error": text_error,
        "text_meta": text_meta,
        "meta_only": meta_only,
        "clean": clean,
        "no_error": no_error,
    }


def _everything_stored(db: Session, *, reload: bool = True) -> str:
    # Reloading throws away changes that were made but not flushed, so a caller that
    # must see such a change (the dry run) reads without it.
    if reload:
        db.expire_all()
    runs = [(r.error, r.meta) for r in db.query(SyncRun).all()]
    types = [(t.error, t.error_code) for t in db.query(SyncRunDataType).all()]
    return json.dumps([runs, types])


class TestReduceSyncRunErrors:
    def test_text_becomes_a_code_and_the_counts_say_how_many(self, db: Session, stored: dict[str, SyncRun]) -> None:
        result = reduce_sync_run_errors(db, dry_run=False)

        assert result == {"runs": 3, "data_types": 2}
        db.expire_all()
        assert stored["text_error"].error == "unclassified"
        assert stored["text_error"].meta == {"is_historical": True}
        assert stored["text_meta"].error == "all_subtasks_failed"
        assert stored["text_meta"].meta == {"params": {"workouts": {"error": "unclassified"}}}
        assert stored["meta_only"].error is None
        assert stored["meta_only"].meta == {"params": {"data_247": {"error": "unclassified"}}}
        types = {(t.run_id, t.data_type): (t.error, t.error_code) for t in db.query(SyncRunDataType).all()}
        assert types[(stored["text_error"].id, "workouts")] == ("unclassified", None)
        assert types[(stored["text_error"].id, "sleep")] == (None, "unclassified")
        assert "187.5" not in _everything_stored(db)

    def test_codes_and_empty_rows_are_left_alone(self, db: Session, stored: dict[str, SyncRun]) -> None:
        reduce_sync_run_errors(db, dry_run=False)

        db.expire_all()
        assert stored["clean"].error == "IntegrityError"
        assert stored["clean"].meta == {"params": {"workouts": {"error": "KeyError"}}}
        assert (stored["no_error"].error, stored["no_error"].meta) == (None, None)
        types = {(t.run_id, t.data_type): (t.error, t.error_code) for t in db.query(SyncRunDataType).all()}
        assert types[(stored["clean"].id, "workouts")] == ("KeyError", "HKError_5")
        assert types[(stored["clean"].id, "sleep")] == (None, None)

    def test_a_second_run_finds_nothing(self, db: Session, stored: dict[str, SyncRun]) -> None:
        reduce_sync_run_errors(db, dry_run=False)

        assert reduce_sync_run_errors(db, dry_run=False) == {"runs": 0, "data_types": 0}

    def test_dry_run_counts_and_changes_nothing(self, db: Session, stored: dict[str, SyncRun]) -> None:
        before = _everything_stored(db)

        result = reduce_sync_run_errors(db, dry_run=True)

        assert result == {"runs": 3, "data_types": 2}
        assert _everything_stored(db, reload=False) == before

    def test_it_prints_counts_and_never_the_text(
        self, db: Session, stored: dict[str, SyncRun], capsys: pytest.CaptureFixture[str]
    ) -> None:
        reduce_sync_run_errors(db, dry_run=False)

        printed = capsys.readouterr().out
        assert "3 run(s)" in printed
        assert "2 per-data-type row(s)" in printed
        assert "187.5" not in printed
        assert "5f0c1c1e" not in printed
