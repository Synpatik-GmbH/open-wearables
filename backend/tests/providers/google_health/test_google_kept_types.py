"""FORK (Calibra 2.72.2, design D-02; spec 2.72 v0.2 section 2.2, AC 3 and AC 9): Google keeps only these types.

Steps, heart rate, daily resting heart rate, HRV samples (RMSSD only), daily HRV and daily breathing
rate; sleep and exercise have their own handlers. Nothing else is requested, so no pull asks for a
nutrition-scope type, and nothing else is stored: no SDNN, temperature, hydration, weight, body
composition, oxygen saturation, VO2 max, distance, energy or glucose.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, SeriesTypeDefinition
from app.schemas.enums import SeriesType
from app.services.providers.google_health.coverage import TIMESERIES
from app.services.providers.google_health.data_247 import GoogleHealth247Data
from app.services.providers.google_health.metrics import DERIVED_DAILY_METRICS, METRICS
from app.services.providers.google_health.webhook_service import GOOGLE_WEBHOOK_DATA_TYPES
from tests.factories import UserFactory

START = datetime(2026, 10, 20, tzinfo=UTC)
END = datetime(2026, 10, 21, tzinfo=UTC)

_INTERVAL = {
    "interval": {"startTime": "2026-10-20T03:00:00Z", "endTime": "2026-10-20T03:01:00Z", "startUtcOffset": "7200s"}
}
_SAMPLE = {"sampleTime": {"physicalTime": "2026-10-20T03:00:00Z", "utcOffset": "7200s"}}
_DATE = {"date": {"year": 2026, "month": 10, "day": 20}}

# Every Google data type the registry read before this build, with one valid point each.
KEPT_POINTS: dict[str, tuple[str, dict[str, Any]]] = {
    "steps": ("steps", {"count": "500", **_INTERVAL}),
    "heart-rate": ("heartRate", {"beatsPerMinute": 60, **_SAMPLE}),
    "daily-resting-heart-rate": ("dailyRestingHeartRate", {"beatsPerMinute": 55, **_DATE}),
    "heart-rate-variability": (
        "heartRateVariability",
        {"rootMeanSquareOfSuccessiveDifferencesMilliseconds": 40, "standardDeviationMilliseconds": 50, **_SAMPLE},
    ),
    "daily-heart-rate-variability": (
        "dailyHeartRateVariability",
        {"averageHeartRateVariabilityMilliseconds": 47, **_DATE},
    ),
    "daily-respiratory-rate": ("dailyRespiratoryRate", {"breathsPerMinute": 15, **_DATE}),
}
DROPPED_POINTS: dict[str, tuple[str, dict[str, Any]]] = {
    "distance": ("distance", {"millimeters": 1_000_000, **_INTERVAL}),
    "active-energy-burned": ("activeEnergyBurned", {"kcal": 100, **_INTERVAL}),
    "hydration-log": ("hydrationLog", {"amountConsumed": {"milliliters": 250}, **_INTERVAL}),
    "run-vo2-max": ("runVo2Max", {"runVo2Max": 45, **_SAMPLE}),
    "vo2-max": ("vo2Max", {"vo2Max": 45, **_SAMPLE}),
    "weight": ("weight", {"weightGrams": 70_000, **_SAMPLE}),
    "body-fat": ("bodyFat", {"percentage": 20, **_SAMPLE}),
    "core-body-temperature": ("coreBodyTemperature", {"celsius": 36.6, **_SAMPLE}),
    "blood-glucose": ("bloodGlucose", {"bloodGlucoseMilligramsPerDeciliter": 90, **_SAMPLE}),
    "oxygen-saturation": ("oxygenSaturation", {"percentage": 97, **_SAMPLE}),
    "daily-oxygen-saturation": ("dailyOxygenSaturation", {"averagePercentage": 97, **_DATE}),
    "total-calories": ("totalCalories", {"kcalSum": 2000}),
}
KEPT_SERIES = {
    SeriesType.steps,
    SeriesType.heart_rate,
    SeriesType.resting_heart_rate,
    SeriesType.heart_rate_variability_rmssd,
    SeriesType.respiratory_rate,
}


def _data_type(endpoint: str) -> str:
    return endpoint.split("/dataTypes/")[1].split("/")[0]


class _FakeGoogle:
    """Answers every data type with one valid point and records what was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def __call__(self, *args: object, **kwargs: object) -> dict[str, Any]:
        data_type = _data_type(str(kwargs.get("endpoint") or args[6]))
        self.asked.append(data_type)
        key, point = {**KEPT_POINTS, **DROPPED_POINTS}[data_type]
        return {"dataPoints": [{key: point}]}


def _stored(db: Session, user_id: Any) -> set[str]:
    rows = db.execute(
        select(SeriesTypeDefinition.code)
        .join(DataPointSeries, DataPointSeries.series_type_definition_id == SeriesTypeDefinition.id)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(DataSource.user_id == user_id)
    )
    return {code for (code,) in rows}


@pytest.fixture
def data_247() -> GoogleHealth247Data:
    return GoogleHealth247Data(oauth=MagicMock(), connection_repo=MagicMock(), api_base_url="https://health.test")


def _pull(data_247: GoogleHealth247Data, run: Any) -> _FakeGoogle:
    fake = _FakeGoogle()
    with (
        patch("app.services.providers.google_health.data_247.make_authenticated_request", side_effect=fake),
        patch.object(data_247.sleep, "load_and_save", return_value=0),
        patch.object(data_247.settings_repo, "get_data_granularity", return_value=None),
        patch("app.services.providers.google_health.data_247.store_raw_payload"),
    ):
        run()
    return fake


def test_the_registry_is_the_spec_2_2_set() -> None:
    assert {m.data_type for m in METRICS} == set(KEPT_POINTS)
    assert DERIVED_DAILY_METRICS == ()


def test_the_hrv_sample_type_writes_rmssd_only() -> None:
    (hrv,) = (m for m in METRICS if m.data_type == "heart-rate-variability")
    assert set(hrv.series_types()) == {SeriesType.heart_rate_variability_rmssd}


def test_coverage_and_the_subscriber_list_follow_the_registry() -> None:
    assert TIMESERIES == KEPT_SERIES
    assert set(GOOGLE_WEBHOOK_DATA_TYPES) <= set(KEPT_POINTS) | {"sleep", "exercise"}


def test_a_pull_asks_for_and_stores_only_the_kept_types(db: Session, data_247: GoogleHealth247Data) -> None:
    member = UserFactory()
    db.flush()

    fake = _pull(data_247, lambda: data_247.load_and_save_all(db, member.id, START, END))

    assert set(fake.asked) == set(KEPT_POINTS)
    assert _stored(db, member.id) == {s.value for s in KEPT_SERIES}


@pytest.mark.parametrize("data_type", sorted(DROPPED_POINTS))
def test_a_notification_for_a_dropped_type_fetches_and_stores_nothing(
    db: Session, data_247: GoogleHealth247Data, data_type: str
) -> None:
    member = UserFactory()
    db.flush()

    fake = _pull(data_247, lambda: data_247.sync_data_type(db, member.id, data_type, START, END))

    assert fake.asked == []
    assert _stored(db, member.id) == set()
