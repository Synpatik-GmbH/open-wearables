"""Garmin data arriving without an active connection is stored nowhere (Calibra 2.71.3.3, D-14; AC 21).

This build changes none of the code these pin. Each case carries its positive control: the same
payload for an active row does write, so "nothing was written" cannot pass for an unrelated reason.
"""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import BaseDbModel
from app.schemas.auth import ConnectionStatus
from app.services.providers.garmin.strategy import GarminStrategy
from tests.factories import UserConnectionFactory, UserFactory

GARMIN_USER = "garmin-push-account"

HRV: dict[str, Any] = {
    "hrv": [
        {
            "userId": GARMIN_USER,
            "summaryId": "x5b70ccc-6966bceb",
            "calendarDate": "2026-01-14",
            "lastNightAvg": 84,
            "lastNight5MinHigh": 124,
            "startTimeOffsetInSeconds": 3600,
            "durationInSeconds": 36565,
            "startTimeInSeconds": 1768340715,
            "hrvValues": {"265": 70, "565": 73, "865": 68},
        }
    ]
}
ACTIVITY: dict[str, Any] = {
    "activities": [
        {
            "userId": GARMIN_USER,
            "summaryId": "21047282990",
            "activityId": 21047282990,
            "activityName": "Morning Run",
            "startTimeInSeconds": 1763597760,
            "durationInSeconds": 3600,
            "startTimeOffsetInSeconds": 3600,
            "activityType": "RUNNING",
            "deviceName": "Forerunner 965",
            "manual": False,
            "isWebUpload": False,
        }
    ]
}
PAYLOADS = [pytest.param(HRV, id="wellness"), pytest.param(ACTIVITY, id="activity")]


def row_counts(db: Session) -> dict[str, int]:
    db.flush()
    return {
        table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
        for table in BaseDbModel.metadata.sorted_tables
    }


def push(db: Session, payload: dict) -> None:
    GarminStrategy().webhooks.process_payload(db, payload, "trace-no-connection")


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_push_whose_only_row_is_revoked_writes_nothing(db: Session, payload: dict) -> None:
    UserConnectionFactory(
        user=UserFactory(), provider="garmin", provider_user_id=GARMIN_USER, status=ConnectionStatus.REVOKED
    )
    before = row_counts(db)

    push(db, payload)

    assert row_counts(db) == before


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_push_for_an_unknown_garmin_user_writes_nothing(db: Session, payload: dict) -> None:
    before = row_counts(db)

    push(db, payload)

    assert row_counts(db) == before


@pytest.mark.parametrize("payload", PAYLOADS)
def test_the_same_push_for_an_active_row_does_write(db: Session, payload: dict) -> None:
    UserConnectionFactory(
        user=UserFactory(), provider="garmin", provider_user_id=GARMIN_USER, status=ConnectionStatus.ACTIVE
    )
    before = row_counts(db)

    push(db, payload)

    assert row_counts(db) != before


def test_raw_payload_storage_stays_disabled_by_default() -> None:
    assert Settings.model_fields["raw_payload_storage"].default == "disabled"
