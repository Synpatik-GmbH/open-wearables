"""2.96.6.1 (R-2026-10-13-e): the three operational tables are deleted with the user.

Calibra erases the OW user on consent withdrawal and on account deletion, so deleting the user
is the path both take.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import PolarPullState, PolarRegistration, PolarWorkoutTrueTime
from app.schemas.enums import ProviderName
from app.services.user_service import user_service
from tests.factories import DataSourceFactory, EventRecordFactory, UserConnectionFactory, UserFactory


@pytest.fixture(autouse=True)
def _no_polar_deregistration(monkeypatch: pytest.MonkeyPatch) -> None:
    # User deletion deregisters every connection with Polar over HTTP first.
    monkeypatch.setattr("app.services.providers.polar.oauth.PolarOAuth.deregister_user", lambda *a, **k: None)


def test_true_time_record_is_deleted_with_the_user(db: Session) -> None:
    user = UserFactory()
    other = UserFactory()
    records = {}
    for owner in (user, other):
        source = DataSourceFactory(user=owner, provider=ProviderName.POLAR, source="polar")
        record = EventRecordFactory(data_source=source, external_id=f"EX-{owner.id}")
        db.add(PolarWorkoutTrueTime(event_record_id=record.id, recorded_by="ingest"))
        records[owner.id] = record.id
    db.commit()

    user_service.delete(db, user.id)

    assert db.get(PolarWorkoutTrueTime, records[user.id]) is None
    assert db.get(PolarWorkoutTrueTime, records[other.id]) is not None


def test_registration_state_is_deleted_with_the_user(db: Session) -> None:
    user = UserFactory()
    other = UserFactory()
    connections = {}
    for owner in (user, other):
        connection = UserConnectionFactory(user=owner, provider="polar")
        db.add(
            PolarRegistration(
                user_connection_id=connection.id,
                registered=False,
                attempts=1,
                last_error_code="HTTPStatusError_503",
                updated_at=datetime.now(timezone.utc),
            )
        )
        connections[owner.id] = connection.id
    db.commit()

    user_service.delete(db, user.id)

    assert db.get(PolarRegistration, connections[user.id]) is None
    assert db.get(PolarRegistration, connections[other.id]) is not None


def test_pull_state_is_deleted_with_the_user(db: Session) -> None:
    user = UserFactory()
    other = UserFactory()
    connections = {}
    for owner in (user, other):
        connection = UserConnectionFactory(user=owner, provider="polar")
        now = datetime.now(timezone.utc)
        db.add(
            PolarPullState(
                user_connection_id=connection.id, pull_started_at=now, full_fetched_at={"sleep": now.isoformat()}
            )
        )
        connections[owner.id] = connection.id
    db.commit()

    user_service.delete(db, user.id)

    assert db.get(PolarPullState, connections[user.id]) is None
    assert db.get(PolarPullState, connections[other.id]) is not None
