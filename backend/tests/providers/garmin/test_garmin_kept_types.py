"""FORK (Calibra 2.71.3.2, spec 2.71.3 v0.3 section 2.2): Garmin keeps only the types Calibra reads.

The Garmin webhook carries every summary type the portal enables. Calibra reads sleeps, HRV
summaries, dailies, epochs, respiration, activities and activity details, plus the permission
and deregistration notices; every other type (Women's Health ``mct`` included, which is 2.71.1)
is stripped before the payload is archived or queued, and again before the worker processes
it. Dailies keep no health score: the only one they produced was stress.

Each dropped type is pushed through the real worker with a payload its builder stores, and the
test reads every table a Garmin summary can reach.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.webhook_push_task import process_webhook_push
from app.models import DataPointSeries, DataSource, EventRecord, HealthScore, SeriesTypeDefinition
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.garmin.webhook_handler import GARMIN_KEPT_TYPES, GarminWebhookHandler
from tests.factories import UserConnectionFactory, UserFactory

MODULE = "app.integrations.celery.tasks.webhook_push_task"
HANDLER_MODULE = "app.services.providers.garmin.webhook_handler"
GARMIN_USER = "garmin_user_123"
T = 1768258800  # 2026-01-12 23:00 UTC

# One payload item per dropped type, each one its builder stores (see the mutation note in the PR).
DROPPED: dict[str, dict[str, Any]] = {
    "stressDetails": {
        "startTimeInSeconds": T,
        "timeOffsetStressLevelValues": {"0": 25, "300": 30},
        "timeOffsetBodyBatteryValues": {"0": 80},
    },
    "pulseox": {"summaryId": "po-1", "startTimeInSeconds": T, "timeOffsetSpo2Values": {"0": 96, "60": 95}},
    "bloodPressures": {"summaryId": "bp-1", "measurementTimeInSeconds": T, "systolic": 120, "diastolic": 80},
    "userMetrics": {"summaryId": "um-1", "calendarDate": "2026-01-12", "vo2Max": 48, "fitnessAge": 30},
    "healthSnapshot": {
        "summaryId": "hs-1",
        "startTimeInSeconds": T,
        "summaries": [
            {"summaryType": "heart_rate", "avgValue": 61},
            {"summaryType": "rmssd_hrv", "avgValue": 52},
            {"summaryType": "stress", "avgValue": 20},
        ],
    },
    "bodyComps": {"summaryId": "bc-1", "measurementTimeInSeconds": T, "weightInGrams": 70000, "bodyFatInPercent": 20},
    "moveIQActivities": {
        "summaryId": "mq-1",
        "startTimeInSeconds": T,
        "durationInSeconds": 900,
        "activityType": "WALKING",
    },
    "mct": {
        "summaryId": "mct-1",
        "periodStartDate": "2026-01-01",
        "cycleLength": 28,
        "dayInCycle": 12,
        "currentPhaseType": "FERTILE_WINDOW",
        "lastUpdatedTimeInSeconds": T,
    },
    "skinTemp": {"summaryId": "st-1", "startTimeInSeconds": T, "skinTemperature": 33.1},
}


@pytest.fixture
def task_db(db: Session) -> Session:
    with (
        patch(f"{MODULE}.SessionLocal", return_value=db),
        patch(f"{MODULE}.ProviderFactory") as mock_factory_cls,
        patch.object(db, "close", MagicMock()),
    ):
        mock_factory_cls.return_value.get_provider.return_value = GarminStrategy()
        yield db


def _member() -> Any:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="garmin", provider_user_id=GARMIN_USER)
    return user


def _rows(db: Session, user_id: Any) -> dict[str, int]:
    """What the member has in every table a Garmin summary can write."""
    series = db.scalar(
        select(func.count())
        .select_from(DataPointSeries)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(DataSource.user_id == user_id)
    )
    events = db.scalar(
        select(func.count())
        .select_from(EventRecord)
        .join(DataSource, DataSource.id == EventRecord.data_source_id)
        .where(DataSource.user_id == user_id)
    )
    scores = db.scalar(select(func.count()).select_from(HealthScore).where(HealthScore.user_id == user_id))
    return {"series": series or 0, "events": events or 0, "scores": scores or 0}


def _codes(db: Session, user_id: Any) -> list[str]:
    rows = db.execute(
        select(SeriesTypeDefinition.code)
        .join(DataPointSeries, DataPointSeries.series_type_definition_id == SeriesTypeDefinition.id)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(DataSource.user_id == user_id)
    )
    return sorted({code for (code,) in rows})


def test_the_kept_set_is_the_spec_2_2_set() -> None:
    assert (
        frozenset(
            {
                "dailies",
                "epochs",
                "sleeps",
                "hrv",
                "allDayRespiration",
                "activities",
                "activityDetails",
                "userPermissionsChange",
                "deregistrations",
            }
        )
        == GARMIN_KEPT_TYPES
    )
    assert set(GarminWebhookHandler(MagicMock(), MagicMock()).supported_event_types()) == GARMIN_KEPT_TYPES


@pytest.mark.parametrize("summary_type", sorted(DROPPED))
def test_a_dropped_type_stores_nothing(
    summary_type: str, task_db: Session, mock_external_apis: dict[str, MagicMock]
) -> None:
    user = _member()
    payload = {summary_type: [{"userId": GARMIN_USER, **DROPPED[summary_type]}]}

    process_webhook_push.run("garmin", payload, f"trace-{summary_type}")

    assert _rows(task_db, user.id) == {"series": 0, "events": 0, "scores": 0}


def test_a_dropped_type_beside_kept_types_leaves_them_stored(
    task_db: Session, mock_external_apis: dict[str, MagicMock]
) -> None:
    user = _member()
    payload = {
        "pulseox": [{"userId": GARMIN_USER, **DROPPED["pulseox"]}],
        "allDayRespiration": [
            {
                "userId": GARMIN_USER,
                "summaryId": "r-1",
                "startTimeInSeconds": T,
                "timeOffsetEpochToBreaths": {"0": 14.0},
            }
        ],
    }

    process_webhook_push.run("garmin", payload, "trace-mixed")

    assert _codes(task_db, user.id) == ["respiratory_rate"]


def test_activity_files_never_reach_the_worker_batch(
    task_db: Session, mock_external_apis: dict[str, MagicMock]
) -> None:
    """activityFiles is held at the dispatch: its storage needs a FIT download no fixture can serve."""
    _member()
    payload = {
        "activityFiles": [
            {"userId": GARMIN_USER, "fileType": "FIT", "callbackURL": "https://example.invalid/f", "activityId": 1}
        ]
    }
    seen: list[str] = []
    with patch(
        "app.services.providers.garmin.data_247.Garmin247Data.process_items_batch",
        side_effect=lambda db, uid, summary_type, items: seen.append(summary_type) or 0,
    ):
        process_webhook_push.run("garmin", payload, "trace-files")

    assert seen == []


def test_dailies_keep_their_series_and_no_stress_score(
    task_db: Session, mock_external_apis: dict[str, MagicMock]
) -> None:
    user = _member()
    payload = {
        "dailies": [
            {
                "userId": GARMIN_USER,
                "summaryId": "daily-1",
                "calendarDate": "2026-01-13",
                "steps": 7694,
                "restingHeartRateInBeatsPerMinute": 43,
                "averageStressLevel": 35,
                "maxStressLevel": 80,
                "stressQualifier": "balanced",
                "startTimeInSeconds": T,
                "startTimeOffsetInSeconds": 3600,
            }
        ]
    }

    process_webhook_push.run("garmin", payload, "trace-dailies")

    rows = _rows(task_db, user.id)
    assert rows["scores"] == 0
    assert {"steps", "resting_heart_rate"} <= set(_codes(task_db, user.id))


def test_the_receive_path_archives_and_queues_only_kept_types() -> None:
    payload = {
        "hrv": [{"userId": GARMIN_USER, "summaryId": "h-1"}],
        "mct": [{"userId": GARMIN_USER, "summaryId": "mct-1"}],
        "skinTemp": [{"userId": GARMIN_USER, "summaryId": "st-1"}],
    }
    with (
        patch(f"{HANDLER_MODULE}.store_raw_payload") as archived,
        patch(f"{HANDLER_MODULE}.celery_app") as celery,
    ):
        celery.send_task.return_value.id = "task-1"
        GarminWebhookHandler(MagicMock(), MagicMock()).dispatch(MagicMock(), payload)

    assert set(archived.call_args.kwargs["payload"]) == {"hrv"}
    assert set(celery.send_task.call_args.kwargs["args"][1]) == {"hrv"}
