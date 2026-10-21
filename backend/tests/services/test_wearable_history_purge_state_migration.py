"""2.96.9.2 migration c4e1d2b7a9f3: purge state per source and XML upload rows."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.factories import UserFactory

PATH = Path(__file__).parents[2] / "migrations/versions/2026_10_21_2300-c4e1d2b7a9f3_wearable_history_purge_state.py"


def migration() -> object:
    spec = importlib.util.spec_from_file_location("history_purge_state", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_source_that_existed_before_the_migration_starts_with_no_purge_recorded(db: Session) -> None:
    module = migration()
    member = UserFactory()
    db.flush()
    with Operations.context(MigrationContext.configure(db.connection())):
        module.downgrade()
    db.execute(
        text(
            "INSERT INTO wearable_history_sources(id,user_id,provider,connection_day,start_date,freeze_at) "
            "VALUES (gen_random_uuid(), :u, 'polar', '2026-10-01', '2026-09-28', '2026-10-07 22:00+00')"
        ),
        {"u": member.id},
    )
    with Operations.context(MigrationContext.configure(db.connection())):
        module.upgrade()
    row = db.execute(
        text(
            "SELECT records_purged_at, purge_completed_at, purge_attempts, purge_last_error "
            "FROM wearable_history_sources WHERE user_id = :u"
        ),
        {"u": member.id},
    ).one()
    assert tuple(row) == (None, None, 0, None)


def test_an_xml_upload_row_starts_pending_and_refuses_an_unknown_state(db: Session) -> None:
    member = UserFactory()
    db.flush()
    db.execute(
        text("INSERT INTO xml_uploads(id,user_id,bucket,object_key) VALUES (gen_random_uuid(), :u, 'b', 'k')"),
        {"u": member.id},
    )
    assert db.scalar(text("SELECT state FROM xml_uploads WHERE user_id = :u"), {"u": member.id}) == "pending_import"
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO xml_uploads(id,user_id,bucket,object_key,state) VALUES (gen_random_uuid(),:u,'b','k2','x')"
            ),
            {"u": member.id},
        )
