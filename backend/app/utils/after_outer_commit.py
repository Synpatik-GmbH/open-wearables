"""Callbacks that run only once the outermost transaction commits.

A SQLAlchemy ``after_commit`` listener also fires when a savepoint is released, and it survives a
rollback, so a webhook registered for rows that are later rolled back still went out. Instead, a
callback is queued on the transaction that is current when it is registered: a released savepoint
hands its queue to its parent, the outermost commit runs it, and a rollback, a failed commit or a
close without commit drops it.
"""

from collections.abc import Callable
from weakref import WeakKeyDictionary

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

_QUEUES: "WeakKeyDictionary[SessionTransaction, list[Callable[[], None]]]" = WeakKeyDictionary()
_COMMITTED = "after_outer_commit.committed"


def run_after_outer_commit(session: Session, callback: Callable[[], None]) -> None:
    """Run ``callback`` after the outermost transaction now open on ``session`` commits."""
    transaction = session.get_nested_transaction() or session.get_transaction() or session.begin()
    _QUEUES.setdefault(transaction, []).append(callback)


def _mark_committed(session: Session) -> None:
    # Fires only for a commit that succeeded, savepoint or outermost, just before that
    # transaction ends; a rollback or close ends it without this mark.
    session.info[_COMMITTED] = True


def _settle(session: Session, transaction: SessionTransaction) -> None:
    committed = session.info.pop(_COMMITTED, False)
    queue = _QUEUES.pop(transaction, None)
    if not queue or not committed:
        return
    if transaction.parent is not None:
        _QUEUES.setdefault(transaction.parent, []).extend(queue)
        return
    for callback in queue:
        callback()


event.listen(Session, "after_commit", _mark_committed)
event.listen(Session, "after_transaction_end", _settle)
