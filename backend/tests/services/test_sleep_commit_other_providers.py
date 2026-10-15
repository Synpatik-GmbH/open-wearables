"""2.96.6.1 (Dragan's review of #40, item 2): a sleep provider that passes no ``before_commit``
still has its night committed by ``create_or_merge_sleep``.

The fork moved the sleep commit out of the five inner branches into one commit in the outer
function, so every sleep provider depends on that line. WHOOP stands for the others: it saves
through ``create_or_merge_sleep`` with no callback, as they do.

The ``db`` fixture runs each test inside a transaction that is rolled back, where a commit only
releases a savepoint, so no other session could see the night either way. This test commits
against the engine itself and reads from a second session, then deletes its user.
"""

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, User
from app.services.providers.whoop.strategy import WhoopStrategy


@pytest.fixture
def committed_user(session_factory: Any) -> Iterator[UUID]:
    user_id = uuid4()
    with session_factory() as session:
        session.add(User(id=user_id, created_at=datetime.now(timezone.utc), email=f"{user_id}@example.test"))
        session.commit()
    try:
        yield user_id
    finally:
        with session_factory() as session:
            session.query(User).filter(User.id == user_id).delete()
            session.commit()


def _night() -> dict[str, Any]:
    return {
        "id": uuid4(),
        "whoop_sleep_id": "whoop-night-1",
        "start_time": "2026-10-13T22:30:00+00:00",
        "end_time": "2026-10-14T06:10:00+00:00",
        "duration_seconds": 7 * 3600 + 40 * 60,
        "efficiency_percent": 91,
        "is_nap": False,
        "stages": {"deep_seconds": 5400, "light_seconds": 12600, "rem_seconds": 6300, "awake_seconds": 3300},
    }


def test_a_whoop_night_is_committed_and_seen_from_another_session(session_factory: Any, committed_user: UUID) -> None:
    writer: Session = session_factory()
    try:
        WhoopStrategy().data_247.save_sleep_data(writer, committed_user, _night())
    finally:
        # Closing rolls back whatever was not committed.
        writer.close()

    with session_factory() as reader:
        nights = (
            reader.query(EventRecord)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(DataSource.user_id == committed_user, EventRecord.category == "sleep")
            .all()
        )

    assert [(n.start_datetime, n.end_datetime) for n in nights] == [
        (datetime(2026, 10, 13, 22, 30, tzinfo=timezone.utc), datetime(2026, 10, 14, 6, 10, tzinfo=timezone.utc))
    ]
