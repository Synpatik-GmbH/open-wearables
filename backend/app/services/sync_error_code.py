"""What a failed sync may keep of its error (fork, Notion 2.47.17.2).

An exception's text is not ours to bound: a database error quotes the statement's
parameters, which are the health values being saved and the user id. A sync event is
stored (sync_run, sync_run_data_type), kept in Redis, logged and sent as a webhook, so
the text would reach all four. They keep a code instead.

A code is the exception's class name, or a fixed word chosen by the caller. It is
checked by shape rather than against a list, since exception classes are open-ended:
it starts with a letter, holds only letters, digits and underscores, is at most 64
characters, and has at most four digits in all. That admits ``HTTPException_404`` and
``OAuth2Error`` and shuts out sentences, numbers, e-mail addresses and ids (a UUID
has hyphens, and without them far more than four digits). Anything else is stored as
``unclassified``, so a caller that passes text by mistake cannot get it kept.
"""

import re
from typing import Any

UNCLASSIFIED = "unclassified"

_CODE = re.compile(r"(?=[A-Za-z])(?!(?:.*\d){5})[A-Za-z0-9_]{1,64}")


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
    if isinstance(error, str) and _CODE.fullmatch(error):
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
