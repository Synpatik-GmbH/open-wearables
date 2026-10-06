"""What a failed sync may keep of its error (fork, Notion 2.47.17.2).

An exception's text is not ours to bound: a database error quotes the statement's
parameters, which are the health values being saved and the user id. A sync event is
stored (sync_run, sync_run_data_type), kept in Redis, logged and sent as a webhook, so
the text would reach all four. They keep a code instead.

A code is one of two things, and nothing is a code by its shape alone:

- the name of an exception class from the registry below, optionally followed by an
  HTTP status (``IntegrityError``, ``HTTPException_404``);
- one of the fixed words below, which callers use where there is no exception.

Everything else is kept as ``unclassified``: sentences, numbers and ids, but also a
single word that merely looks like a code, such as a name or the error code a phone
reports for itself. Callers still pass strings, so this is where a caller that passes
text by mistake is stopped.

The registry is the same in every process, because a worker stores a code and the
start-up script, which has loaded far less, reads it back: it is the exception classes
that a fixed list of modules export, not the classes a process happens to have loaded.
An exception of a class outside it is ``unclassified`` too.
"""

import importlib
import re
from typing import Any

UNCLASSIFIED = "unclassified"
UNKNOWN = "unknown"
ALL_SUBTASKS_FAILED = "all_subtasks_failed"
SDK_IMPORT_FAILED = "sdk_import_failed"

_FIXED_WORDS = frozenset({UNCLASSIFIED, UNKNOWN, ALL_SUBTASKS_FAILED, SDK_IMPORT_FAILED})

# Every module of this backend that defines an exception class. A test finds the classes
# and fails when one is missing from here.
_APP_MODULES = (
    "app.utils.exceptions",
    "app.integrations.celery.tasks.send_email_task",
    "app.services.providers.google_health.data_247",
    "app.services.providers.withings.handlers.rpc_client",
    "app.services.providers.withings.oauth",
)
# The libraries a sync runs on. A class from anywhere else reads "unclassified".
_LIBRARY_MODULES = (
    "builtins",
    "asyncio",
    "binascii",
    "concurrent.futures",
    "decimal",
    "json",
    "socket",
    "ssl",
    "botocore.exceptions",
    "celery.exceptions",
    "fastapi",
    "fastapi.exceptions",
    "httpcore",
    "httpx",
    "kombu.exceptions",
    "psycopg",
    "psycopg.errors",
    "pydantic",
    "pydantic_core",
    "redis.exceptions",
    "requests.exceptions",
    "sqlalchemy.exc",
    "sqlalchemy.orm.exc",
    "starlette.exceptions",
    "urllib3.exceptions",
)

_WITH_STATUS = re.compile(r"(?P<name>.+)_(?P<status>[1-5][0-9]{2})")

_registry: frozenset[str] | None = None


def _registered_exception_names() -> frozenset[str]:
    # Built on first use, by name, and so imported here rather than at the top of the
    # file: the provider modules import their way back to this one.
    global _registry
    if _registry is None:
        names: set[str] = set()
        for module_name in (*_APP_MODULES, *_LIBRARY_MODULES):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                # This runs inside emit(), which must not raise. A module that has gone
                # missing costs its own codes, which then read "unclassified"; the test
                # that asks a fresh interpreter is what reports it.
                continue
            exported = vars(module).values()
            names.update(
                item.__name__ for item in exported if isinstance(item, type) and issubclass(item, BaseException)
            )
        _registry = frozenset(names)
    return _registry


def _is_exception_name(name: str) -> bool:
    return name in _registered_exception_names()


def _is_code(text: str) -> bool:
    if text in _FIXED_WORDS or _is_exception_name(text):
        return True
    with_status = _WITH_STATUS.fullmatch(text)
    return with_status is not None and _is_exception_name(with_status["name"])


def error_code(error: object) -> str | None:
    """The code to keep for ``error``: an exception, a code, or text that is dropped."""
    if error is None:
        return None
    if isinstance(error, BaseException):
        name = type(error).__name__
        status = getattr(error, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            name = f"{name}_{status}"
        error = name
    if isinstance(error, str) and _is_code(error):
        return error
    return UNCLASSIFIED


def without_error_text(value: Any) -> Any:
    """``value`` with whatever sits under a key named ``error``, at any depth, as a code.

    Producers nest a task's outcome in an event's metadata as
    ``{"success": False, "error": str(exc)}``. None and booleans are left as they are.
    """
    if isinstance(value, dict):
        return {key: _as_code(item) if key == "error" else without_error_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [without_error_text(item) for item in value]
    return value


def _as_code(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    return error_code(value)
