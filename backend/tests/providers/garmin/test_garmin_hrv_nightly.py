"""FORK (Calibra 2.71.3.2, spec 2.71.3 v0.3 P3 and P4): Garmin's nightly HRV value is its own series.

An HRV summary's ``lastNightAvg`` is Garmin's own overnight value, and the adapter reads it as
the night's Garmin HRV. It is stored as ``garmin_hrv_rmssd_nightly``, never in the RMSSD series
that holds the summary's 5-minute values: in one series the two could not be told apart over
REST, and a 5-minute value at offset 0 shares the summary's start instant and would replace it.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.constants.webhooks.events import SERIES_TYPE_TO_GRANULAR_EVENT
from app.integrations.celery.tasks.webhook_push_task import process_webhook_push
from app.models import DataPointSeries, DataSource, SeriesTypeDefinition
from app.schemas.enums import SeriesType
from app.schemas.model_crud.activities.data_point_series import TimeSeriesQueryParams
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.timeseries_service import timeseries_service
from tests.factories import UserConnectionFactory, UserFactory

MODULE = "app.integrations.celery.tasks.webhook_push_task"
GARMIN_USER = "garmin_user_123"
START = 1768255800  # 2026-01-12 22:10 UTC, 23:10 at +01:00
NIGHTLY = SeriesType.garmin_hrv_rmssd_nightly
EVENT = "series.garmin_hrv_rmssd_nightly.created"


@pytest.fixture
def task_db(db: Session) -> Session:
    with (
        patch(f"{MODULE}.SessionLocal", return_value=db),
        patch(f"{MODULE}.ProviderFactory") as mock_factory_cls,
        patch.object(db, "close", MagicMock()),
    ):
        mock_factory_cls.return_value.get_provider.return_value = GarminStrategy()
        yield db


def _summary(**overrides: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "userId": GARMIN_USER,
        "summaryId": "hrv-1",
        "calendarDate": "2026-01-13",
        "lastNightAvg": 84,
        "lastNight5MinHigh": 124,
        "startTimeInSeconds": START,
        "startTimeOffsetInSeconds": 3600,
        "durationInSeconds": 27000,
        "hrvValues": {"0": 40, "300": 45},
    }
    summary.update(overrides)
    return {k: v for k, v in summary.items() if v is not None}


def _push(db: Session, summary: dict[str, Any]) -> Any:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="garmin", provider_user_id=GARMIN_USER)
    process_webhook_push.run("garmin", {"hrv": [summary]}, "trace-hrv")
    return user


def _stored(db: Session, user_id: Any) -> list[tuple[str, datetime, Decimal, str | None]]:
    rows = db.execute(
        select(
            SeriesTypeDefinition.code, DataPointSeries.recorded_at, DataPointSeries.value, DataPointSeries.zone_offset
        )
        .join(DataPointSeries, DataPointSeries.series_type_definition_id == SeriesTypeDefinition.id)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(DataSource.user_id == user_id)
        .order_by(SeriesTypeDefinition.code, DataPointSeries.recorded_at)
    )
    return [(code, at, value, zone) for code, at, value, zone in rows]


def test_the_nightly_value_is_its_own_series_and_the_five_minute_values_stay_rmssd(
    task_db: Session, mock_external_apis: dict[str, MagicMock]
) -> None:
    user = _push(task_db, _summary())

    start = datetime.fromtimestamp(START, tz=timezone.utc)
    assert _stored(task_db, user.id) == [
        (NIGHTLY.value, start, Decimal("84.000"), "+01:00"),
        ("heart_rate_variability_rmssd", start, Decimal("40.000"), "+01:00"),
        (
            "heart_rate_variability_rmssd",
            datetime.fromtimestamp(START + 300, tz=timezone.utc),
            Decimal("45.000"),
            "+01:00",
        ),
    ]


def test_a_summary_without_a_nightly_value_writes_no_nightly_row(
    task_db: Session, mock_external_apis: dict[str, MagicMock]
) -> None:
    user = _push(task_db, _summary(lastNightAvg=None))

    assert [code for code, *_ in _stored(task_db, user.id)] == ["heart_rate_variability_rmssd"] * 2


def test_a_summary_without_a_start_writes_nothing(task_db: Session, mock_external_apis: dict[str, MagicMock]) -> None:
    user = _push(task_db, _summary(startTimeInSeconds=None))

    assert _stored(task_db, user.id) == []


def test_the_rest_read_serves_the_nightly_value_with_its_offset(
    task_db: Session, mock_external_apis: dict[str, MagicMock]
) -> None:
    user = _push(task_db, _summary())

    page = timeseries_service.get_timeseries(
        task_db,
        user.id,
        [NIGHTLY],
        TimeSeriesQueryParams(
            start_datetime=datetime(2026, 1, 12, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 1, 14, tzinfo=timezone.utc),
        ),
    )

    assert [(s.type, s.value, s.zone_offset, s.source.provider if s.source else None) for s in page.data] == [
        (NIGHTLY, 84, "+01:00", "garmin")
    ]


def test_the_nightly_series_is_announced_on_the_priority_lane() -> None:
    assert SERIES_TYPE_TO_GRANULAR_EVENT[NIGHTLY.value] == EVENT
    assert EVENT in settings.webhook_priority_event_set
