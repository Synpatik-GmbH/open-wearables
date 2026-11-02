"""Google Health data arriving without an active connection is stored nowhere
(Calibra 2.72.3, design D-10, INV-07; spec 2.7 and AC 25).

This build changes none of the code these pin. A Google notification carries no data: the handler
fetches it for the row it resolves. So each case carries its positive control: the same
notification for an active row reaches the fetch, and "nothing was written" cannot pass because
the notification was malformed.
"""

from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import BaseDbModel
from app.schemas.auth import ConnectionStatus
from app.services.providers.google_health.strategy import GoogleHealthStrategy
from app.services.providers.google_health.webhook_handler import GoogleWebhookHandler
from tests.factories import UserConnectionFactory, UserFactory

HEALTH_USER = "google-push-account"


def notification(data_type: str) -> list[dict[str, Any]]:
    return [
        {
            "data": {
                "healthUserId": HEALTH_USER,
                "operation": "UPSERT",
                "dataType": data_type,
                "intervals": [
                    {
                        "physicalTimeInterval": {
                            "startTime": "2026-10-29T00:00:00Z",
                            "endTime": "2026-10-29T08:00:00Z",
                        }
                    }
                ],
            }
        }
    ]


NOTIFICATIONS = [
    pytest.param("sleep", id="sleep"),
    pytest.param("exercise", id="exercise"),
    pytest.param("daily-heart-rate-variability", id="daily-hrv"),
    pytest.param("steps", id="steps"),
]


def row_counts(db: Session) -> dict[str, int]:
    db.flush()
    return {
        table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
        for table in BaseDbModel.metadata.sorted_tables
    }


def push(db: Session, data_type: str) -> list[tuple[Any, ...]]:
    fetched: list[tuple[Any, ...]] = []
    with patch.object(GoogleWebhookHandler, "_fetch_and_save", side_effect=lambda *a: fetched.append(a) or 0):
        GoogleHealthStrategy().webhooks.process_payload(db, notification(data_type), "trace-no-connection")
    return fetched


@pytest.mark.parametrize("data_type", NOTIFICATIONS)
def test_a_notification_whose_only_row_is_revoked_fetches_and_writes_nothing(db: Session, data_type: str) -> None:
    UserConnectionFactory(
        user=UserFactory(), provider="google_health", provider_user_id=HEALTH_USER, status=ConnectionStatus.REVOKED
    )
    before = row_counts(db)

    assert push(db, data_type) == []
    assert row_counts(db) == before


@pytest.mark.parametrize("data_type", NOTIFICATIONS)
def test_a_notification_for_an_unknown_health_user_fetches_and_writes_nothing(db: Session, data_type: str) -> None:
    before = row_counts(db)

    assert push(db, data_type) == []
    assert row_counts(db) == before


@pytest.mark.parametrize("data_type", NOTIFICATIONS)
def test_the_same_notification_for_an_active_row_reaches_the_fetch(db: Session, data_type: str) -> None:
    connection = UserConnectionFactory(
        user=UserFactory(), provider="google_health", provider_user_id=HEALTH_USER, status=ConnectionStatus.ACTIVE
    )

    fetched = push(db, data_type)

    # Patched on the class, so the call carries (db, user_id, data_type, start, end) with no self.
    assert len(fetched) == 1
    assert (fetched[0][1], fetched[0][2]) == (connection.user_id, data_type)


def test_raw_payload_storage_stays_disabled_by_default() -> None:
    assert Settings.model_fields["raw_payload_storage"].default == "disabled"
