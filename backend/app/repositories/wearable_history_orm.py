from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

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
