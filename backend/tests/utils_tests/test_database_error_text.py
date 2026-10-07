"""
FORK (data protection, Notion 2.47.17.3, ruling R-2026-10-07-a): a failed save leaves an
error code in the logs, never the database's message.

The database's message for a failed save quotes what was being saved: a unique violation
names the key's values, a failed check prints the whole row, a bad cast prints the input.
SQLAlchemy adds the statement and its parameters. Hundreds of log calls write that text,
most of them in handlers that catch any error, so it is reduced once, where every database
error is raised: the text becomes the error's name, its SQLSTATE and the names of the
constraint, table and column involved.
"""

import logging
import traceback
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg.errors
import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, OperationalError, StatementError
from sqlalchemy.orm import Session

import app.database  # noqa: F401  (importing it is what installs the reduction)
from app.models import SyncRun
from app.schemas.sync_status import SyncScope, SyncSource, SyncStatus
from app.utils.database_error_text import reduce_database_error
from tests.factories import UserFactory

# What a log line must never show. The weight stands for any health value.
WEIGHT = "81.4"
USER_ID = str(uuid4())

TABLE = """
create temp table probe (
    id uuid primary key,
    user_id uuid not null,
    weight numeric(5, 1) not null constraint probe_weight_positive check (weight > 0),
    note varchar(4),
    ref uuid constraint probe_ref_fkey references probe (id),
    constraint probe_user_weight_key unique (user_id, weight)
)
"""
INSERT = "insert into probe (id, user_id, weight, note, ref) values (:id, :user_id, :weight, :note, :ref)"


def _row(**overrides: Any) -> dict[str, Any]:
    return {"id": str(uuid4()), "user_id": USER_ID, "weight": 81.4, "note": None, "ref": None, **overrides}


# Each: the statements to run, the SQLAlchemy class raised, the psycopg class behind it,
# its SQLSTATE, and a name the reduced text keeps.
FAILED_SAVES: dict[str, tuple[list[tuple[str, dict[str, Any]]], type, type, str, str | None]] = {
    "the same key twice": (
        [(INSERT, _row()), (INSERT, _row())],
        IntegrityError,
        psycopg.errors.UniqueViolation,
        "23505",
        "probe_user_weight_key",
    ),
    "a missing value": (
        [(INSERT, _row(user_id=None, note=WEIGHT))],
        IntegrityError,
        psycopg.errors.NotNullViolation,
        "23502",
        "user_id",
    ),
    "a failed check": (
        [(INSERT, _row(weight=-81.4))],
        IntegrityError,
        psycopg.errors.CheckViolation,
        "23514",
        "probe_weight_positive",
    ),
    "a missing parent": (
        [(INSERT, _row(ref=USER_ID))],
        IntegrityError,
        psycopg.errors.ForeignKeyViolation,
        "23503",
        "probe_ref_fkey",
    ),
    "text that is not a number": (
        [
            (
                "insert into probe (id, user_id, weight) values (:id, :user_id, cast(:w as numeric))",
                {**_row(), "w": "81.4kg"},
            )
        ],
        DataError,
        psycopg.errors.InvalidTextRepresentation,
        "22P02",
        None,
    ),
    "a number too large": (
        [(INSERT, _row(weight=99999981.4))],
        DataError,
        psycopg.errors.NumericValueOutOfRange,
        "22003",
        None,
    ),
    "text too long": (
        [(INSERT, _row(note="81.4-long"))],
        DataError,
        psycopg.errors.StringDataRightTruncation,
        "22001",
        None,
    ),
}


def _fail(db: Session, statements: list[tuple[str, dict[str, Any]]]) -> DBAPIError:
    """Run the statements; the last one is the save that fails."""
    db.execute(text(TABLE))
    for statement, params in statements[:-1]:
        db.execute(text(statement), params)
    statement, params = statements[-1]
    with pytest.raises(DBAPIError) as raised:
        db.execute(text(statement), params)
    return raised.value


def _everything_a_log_could_show(error: BaseException) -> str:
    shown = [str(error), repr(error), "".join(traceback.format_exception(error))]
    cause = error.__cause__
    while cause is not None:
        shown += [str(cause), repr(cause)]
        cause = cause.__cause__
    return "\n".join(shown)


@pytest.mark.parametrize("failure", FAILED_SAVES)
class TestAFailedSave:
    def test_its_text_holds_no_value_and_no_user_id(self, db: Session, failure: str) -> None:
        error = _fail(db, FAILED_SAVES[failure][0])

        shown = _everything_a_log_could_show(error)

        assert WEIGHT not in shown
        assert USER_ID not in shown

    def test_the_error_inside_it_holds_none_either(self, db: Session, failure: str) -> None:
        """The driver's own error is what a traceback prints as the cause."""
        error = _fail(db, FAILED_SAVES[failure][0])

        assert WEIGHT not in str(error.orig)
        assert USER_ID not in str(error.orig)
        assert WEIGHT not in repr(error.orig.args)  # ty: ignore[unresolved-attribute]

    def test_it_carries_neither_the_statement_nor_its_parameters(self, db: Session, failure: str) -> None:
        error = _fail(db, FAILED_SAVES[failure][0])

        assert error.statement is None
        assert error.params is None
        assert "[SQL" not in str(error)
        assert "parameters" not in str(error)

    def test_it_is_still_the_same_kind_of_error(self, db: Session, failure: str) -> None:
        """Code that reacts to a duplicate checks the class, here and on ``orig``."""
        _, sqlalchemy_class, driver_class, _, _ = FAILED_SAVES[failure]

        error = _fail(db, FAILED_SAVES[failure][0])

        assert type(error) is sqlalchemy_class
        assert type(error.orig) is driver_class

    def test_its_text_is_the_name_the_sqlstate_and_what_was_involved(self, db: Session, failure: str) -> None:
        _, _, driver_class, sqlstate, name = FAILED_SAVES[failure]

        error = _fail(db, FAILED_SAVES[failure][0])

        assert str(error.orig).startswith(f"{driver_class.__name__} sqlstate={sqlstate}")
        assert sqlstate in str(error)
        if name is not None:
            assert name in str(error)

    def test_a_log_call_that_writes_the_error_shows_none_of_it(
        self, db: Session, failure: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The two shapes the code base uses: the error in the message, and a traceback."""
        logger = logging.getLogger("test.failed_save")
        try:
            raise _fail(db, FAILED_SAVES[failure][0])
        except DBAPIError as error:
            with caplog.at_level(logging.ERROR, logger="test.failed_save"):
                logger.error(f"Failed to save sample: {error}")
                logger.error("Failed to save sample", exc_info=True)

        assert len(caplog.records) == 2
        assert FAILED_SAVES[failure][3] in caplog.text
        assert WEIGHT not in caplog.text
        assert USER_ID not in caplog.text


class TestOtherWaysASaveFails:
    def test_a_save_through_the_orm(self, db: Session) -> None:
        """The path every repository takes: add, then flush."""
        user = UserFactory()
        run_key = f"pull_{WEIGHT}_marker"

        def run() -> SyncRun:
            now = user.created_at
            return SyncRun(
                id=uuid4(),
                run_key=run_key,
                user_id=user.id,
                provider="oura",
                source=SyncSource.PULL,
                scope=SyncScope.HISTORICAL,
                status=SyncStatus.SUCCESS,
                started_at=now,
                updated_at=now,
                items_inserted=0,
                items_updated=0,
            )

        db.add(run())
        db.flush()
        db.add(run())
        with pytest.raises(IntegrityError) as raised:
            db.flush()

        shown = _everything_a_log_could_show(raised.value)
        assert run_key not in shown
        assert str(user.id) not in shown
        assert "23505" in shown

    def test_an_error_raised_before_the_statement_reaches_the_database(self, db: Session) -> None:
        """SQLAlchemy's own error for a statement also prints the parameters."""
        db.execute(text(TABLE))
        complete, incomplete = _row(), _row()
        del incomplete["weight"]

        with pytest.raises(StatementError) as raised:
            db.execute(text(INSERT), [complete, incomplete])

        shown = _everything_a_log_could_show(raised.value)
        assert WEIGHT not in shown
        assert USER_ID not in shown
        assert type(raised.value.orig).__name__ in str(raised.value)
        assert raised.value.statement is None
        assert raised.value.params is None

    def test_what_code_may_still_ask_the_driver(self, db: Session) -> None:
        """Only the text is reduced. The fields stay, for code that needs to know which constraint."""
        error = _fail(db, FAILED_SAVES["the same key twice"][0])

        assert error.orig.diag.constraint_name == "probe_user_weight_key"  # ty: ignore[unresolved-attribute]
        assert error.orig.sqlstate == "23505"  # ty: ignore[unresolved-attribute]


class TestAConnectionThatFails:
    """Not a failed save: the database never answered, so the text is the driver's own
    account of the connection (host, port, reason) and it is what tells an outage apart."""

    def test_the_reason_is_kept(self) -> None:
        nowhere = create_engine("postgresql+psycopg://u:p@127.0.0.1:1/db", connect_args={"connect_timeout": 2})

        with pytest.raises(OperationalError) as raised:
            nowhere.connect()

        assert "127.0.0.1" in str(raised.value)
        assert type(raised.value.orig) is psycopg.OperationalError

    @pytest.mark.parametrize("driver_class", [psycopg.OperationalError, psycopg.errors.ConnectionTimeout])
    def test_one_lost_during_a_statement_keeps_its_reason_and_loses_the_statement(
        self, driver_class: type[psycopg.OperationalError]
    ) -> None:
        original = driver_class("consuming input failed: server closed the connection unexpectedly")
        wrapped = OperationalError(INSERT, _row(), original, connection_invalidated=True)
        context = SimpleNamespace(sqlalchemy_exception=wrapped, original_exception=original)

        reduced = reduce_database_error(context)  # ty: ignore[invalid-argument-type]

        assert isinstance(reduced, OperationalError)
        assert "server closed the connection" in str(reduced)
        assert reduced.statement is None
        assert reduced.params is None
        assert reduced.connection_invalidated is True
        shown = _everything_a_log_could_show(reduced)
        assert WEIGHT not in shown
        assert USER_ID not in shown

    @pytest.mark.parametrize(
        "original",
        [
            # The database did answer: this one has a SQLSTATE, so its text is the database's.
            psycopg.errors.AdminShutdown(f"terminating connection, last value {WEIGHT}"),
            # The driver's own, but not about the connection.
            psycopg.DataError(f"cannot dump {WEIGHT}"),
            psycopg.ProgrammingError(f"cannot adapt {WEIGHT}"),
        ],
        ids=lambda original: type(original).__name__,
    )
    def test_no_other_error_keeps_its_text(self, original: psycopg.Error) -> None:
        wrapped = DBAPIError(INSERT, _row(), original)
        context = SimpleNamespace(sqlalchemy_exception=wrapped, original_exception=original)

        reduced = reduce_database_error(context)  # ty: ignore[invalid-argument-type]

        assert reduced is not None
        assert WEIGHT not in _everything_a_log_could_show(reduced)


class TestThePoolStillRecovers:
    def test_a_connection_the_database_closed_is_replaced_without_an_error(self, engine: Engine) -> None:
        """pool_pre_ping reads connection_invalidated off the error this module returns. If
        that were lost, the first request after the database dropped a connection would fail."""
        pooled = create_engine(engine.url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        try:
            with pooled.connect() as connection:
                backend = connection.execute(text("select pg_backend_pid()")).scalar_one()
            with engine.connect() as other:
                other.execute(text("select pg_terminate_backend(:pid)"), {"pid": backend})

            with pooled.connect() as connection:
                replacement = connection.execute(text("select pg_backend_pid()")).scalar_one()
        finally:
            pooled.dispose()

        assert replacement != backend


class TestWhereItIsInstalled:
    def test_on_every_engine_in_the_process(self) -> None:
        """On the Engine class, so no engine can be created without it: the app's two,
        the one a script makes, the one these tests run on."""
        assert event.contains(Engine, "handle_error", reduce_database_error)

    def test_an_error_that_is_not_about_a_statement_is_left_alone(self) -> None:
        original = RuntimeError(f"weight {WEIGHT}")
        context = SimpleNamespace(sqlalchemy_exception=None, original_exception=original)

        assert reduce_database_error(context) is None  # ty: ignore[invalid-argument-type]
        assert original.args == (f"weight {WEIGHT}",)

    def test_a_lost_connection_is_still_marked_as_one(self) -> None:
        """The pool reads this flag to throw the connection away."""
        original = psycopg.errors.AdminShutdown(f"terminating connection, last value {WEIGHT}")
        wrapped = DBAPIError("select 1", {"weight": WEIGHT}, original, connection_invalidated=True)
        context = SimpleNamespace(sqlalchemy_exception=wrapped, original_exception=original)

        reduced = reduce_database_error(context)  # ty: ignore[invalid-argument-type]

        assert isinstance(reduced, DBAPIError)
        assert reduced.connection_invalidated is True
        assert WEIGHT not in _everything_a_log_could_show(reduced)
