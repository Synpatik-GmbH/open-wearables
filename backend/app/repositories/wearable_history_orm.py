from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from app.models import DataPointSeries, EventRecord, EventRecordDetail, HealthScore
from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository
from app.utils.wearable_history_ingress import PreStartDataRejectedError


def enforce_wearable_history(session: Session, _context: Any, _instances: Any) -> None:
    observations = [*session.new, *session.dirty]
    events = [record for record in observations if isinstance(record, EventRecord)]
    gate = WearableHistoryIngressRepository(session)
    gate.observe_events(events, flush=False)
    gate.prime_samples([sample for sample in observations if isinstance(sample, DataPointSeries)])
    for observation in observations:
        if isinstance(observation, EventRecord):
            admitted = gate.event_admitted(observation)
        elif isinstance(observation, DataPointSeries):
            admitted = gate.sample_admitted(observation, events)
        elif isinstance(observation, EventRecordDetail):
            parent = session.get(EventRecord, observation.record_id)
            admitted = parent is None or gate.event_admitted(parent)
        elif isinstance(observation, HealthScore):
            admitted = gate.score_admitted(observation)
        else:
            continue
        if not admitted:
            raise PreStartDataRejectedError("Frozen source rejects a pre-start observation")


event.listen(Session, "before_flush", enforce_wearable_history)


# after_commit listeners (outgoing webhooks, workout finalizers) live on the session, and a
# rollback does not remove them, so a caller that rolls back an item or a transaction and goes
# on to commit would announce rows that no longer exist. Each transaction or savepoint records
# how many listeners existed when it began; its rollback drops the ones added since. Listeners
# are only appended (a fired `once` listener stays attached), so a count is enough. SQLAlchemy
# has no public removal for a `once` listener, hence the collection's own deque.
_LISTENERS_AT_BEGIN: "WeakKeyDictionary[SessionTransaction, int]" = WeakKeyDictionary()


def _after_commit_listeners(session: Session) -> Any:
    collection = getattr(session.dispatch, "after_commit")  # noqa: B009 (typed as a union)
    return getattr(collection, "listeners", None)


def remember_after_commit_listeners(session: Session, transaction: SessionTransaction) -> None:
    listeners = _after_commit_listeners(session)
    _LISTENERS_AT_BEGIN[transaction] = len(listeners) if listeners is not None else 0


def drop_rolled_back_after_commit_listeners(session: Session, previous_transaction: SessionTransaction) -> None:
    at_begin = _LISTENERS_AT_BEGIN.pop(previous_transaction, None)
    listeners = _after_commit_listeners(session)
    if at_begin is None or listeners is None:
        return
    while len(listeners) > at_begin:
        listeners.pop()


event.listen(Session, "after_transaction_create", remember_after_commit_listeners)
event.listen(Session, "after_soft_rollback", drop_rolled_back_after_commit_listeners)
