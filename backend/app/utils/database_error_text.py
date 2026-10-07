"""FORK (data protection, Notion 2.47.17.3, ruling R-2026-10-07-a): a database error's
text is a code, never the database's message.

The message for a failed save quotes what was being saved (a unique violation names the
key's values, a failed check prints the row), and SQLAlchemy appends the statement and its
parameters. Hundreds of log calls write an error's text, most of them in handlers that
catch any error, so the text is reduced here, where every such error is raised, and not at
each log call.
"""

import psycopg
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.exc import DBAPIError, StatementError


def reduce_database_error(context: ExceptionContext) -> StatementError | None:
    """Replace the error SQLAlchemy is about to raise with one whose text holds no values.

    The class of the error and of the driver's error inside it (``orig``) are kept, since
    code tells a duplicate from a real failure by them. So are the driver's fields
    (``orig.diag``, ``orig.sqlstate``). The driver's error is changed in place and not
    replaced, because a traceback prints it as the cause.
    """
    raised = context.sqlalchemy_exception
    if raised is None:
        return None

    original = raised.orig
    if original is None:
        return None
    if not _is_about_the_connection(original):
        original.args = (_reduced_text(original),)

    if isinstance(raised, DBAPIError):
        return type(raised)(
            None,
            None,
            original,
            hide_parameters=True,
            connection_invalidated=raised.connection_invalidated,
            ismulti=raised.ismulti,
        )
    return type(raised)(type(original).__name__, None, None, original, hide_parameters=True, ismulti=raised.ismulti)


def _is_about_the_connection(original: BaseException) -> bool:
    """Whether the driver is reporting on the connection and the database sent no message.

    That text is the driver's own (host, port, why it could not connect or was cut off)
    and is what tells an outage apart. Only this one case keeps its text: with a SQLSTATE
    the text is the database's, and the driver's other errors are about a value.
    """
    return isinstance(original, psycopg.OperationalError) and original.sqlstate is None


def _reduced_text(original: BaseException) -> str:
    """The error's name, its SQLSTATE, and the names of what was involved.

    Names of constraints, tables and columns come from the schema, not from a row.
    """
    parts = [type(original).__name__]
    diag = getattr(original, "diag", None)
    for label, value in (
        ("sqlstate", getattr(original, "sqlstate", None)),
        ("constraint", getattr(diag, "constraint_name", None)),
        ("table", getattr(diag, "table_name", None)),
        ("column", getattr(diag, "column_name", None)),
    ):
        if value:
            parts.append(f"{label}={value}")
    return " ".join(parts)
