"""2.96.6.1: Apple Health's sleep save commits where it did before the Polar sleep change.

The Polar fix for Codex 4270734341 made ``event_record_service.create_or_merge_sleep`` commit once,
through an opt-in callback, for every provider that calls it (Andrei confirmed the deviation on
14 Oct 2026). The Apple SDK path, ``sleep_service.finish_sleep``, writes its night through other
calls. This pins the order of its writes and commits, recorded on ``release/0.9.0-syn`` and
unchanged here, so a change to the shared sleep write that reached Apple would show.
"""

from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.orm import Session

from app.constants.sleep import SleepStageType
from app.services.event_record_service import event_record_service
from app.services.sdk.sleep_service import SleepState, SleepStateStage, finish_sleep
from tests.factories import UserFactory


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def test_apple_sleep_save_commits_at_the_same_points(db: Session) -> None:
    user = UserFactory()
    db.commit()
    state = SleepState(
        uuid=str(uuid4()),
        source_name="Apple Watch",
        device_model="Watch7,5",
        provider="apple",
        start_time=_dt("2026-10-13T21:30:00Z"),
        end_time=_dt("2026-10-14T05:00:00Z"),
        last_start_timestamp=_dt("2026-10-14T02:00:00Z"),
        last_end_timestamp=_dt("2026-10-14T05:00:00Z"),
        sleeping_seconds=27000.0,
        stages=[
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-10-13T21:30:00Z"),
                end_time=_dt("2026-10-14T05:00:00Z"),
            ),
        ],
    )
    events: list[str] = []
    real_commit = db.commit
    real_create = event_record_service.create
    real_create_detail = event_record_service.create_detail

    def commit() -> None:
        events.append("commit")
        real_commit()

    def create(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        events.append("create")
        return real_create(*args, **kwargs)

    def create_detail(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        events.append("create_detail")
        return real_create_detail(*args, **kwargs)

    with (
        patch.object(db, "commit", side_effect=commit),
        patch.object(event_record_service, "create", side_effect=create),
        patch.object(event_record_service, "create_detail", side_effect=create_detail),
        patch("app.services.sdk.sleep_service.delete_sleep_state"),
    ):
        finish_sleep(db, str(user.id), state)

    assert events == EXPECTED, events


# Recorded on release/0.9.0-syn at 62e5048c, the base of this branch, with this same test.
EXPECTED: list[str] = ["create", "commit", "commit", "create_detail", "commit"]
