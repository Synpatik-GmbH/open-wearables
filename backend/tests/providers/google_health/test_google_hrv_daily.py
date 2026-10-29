"""FORK (Calibra 2.72.2, design D-01; spec 2.72 v0.2 P3, P4): Google's daily HRV value is its own series.

``daily-heart-rate-variability`` was written into ``heart_rate_variability_rmssd`` beside the HRV
samples. The REST read cannot tell them apart, and a sample at 00:00 UTC shares the daily value's
instant, so a batch could keep the sample and lose the night's value. It is now
``google_hrv_rmssd_daily`` (ms, ID 599), stamped at 00:00 UTC of Google's date with no offset and
announced on the priority lane; the samples stay RMSSD.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.constants.webhooks.events import SERIES_TYPE_TO_GRANULAR_EVENT
from app.models import DataPointSeries, DataSource, SeriesTypeDefinition
from app.schemas.enums import SeriesType
from app.schemas.enums.series_types import SERIES_TYPE_DEFINITIONS
from app.schemas.model_crud.activities.data_point_series import TimeSeriesQueryParams
from app.services.providers.google_health.data_247 import GoogleHealth247Data
from app.services.timeseries_service import timeseries_service
from tests.factories import UserFactory
from tests.providers.google_health.history import google_source

DAILY = SeriesType.google_hrv_rmssd_daily
EVENT = "series.google_hrv_rmssd_daily.created"
START = datetime(2026, 10, 20, tzinfo=UTC)
END = datetime(2026, 10, 21, tzinfo=UTC)
MIDNIGHT = datetime(2026, 10, 20, tzinfo=UTC)
CONNECTED_AT = datetime(2026, 10, 20, 8, tzinfo=UTC)


def _sample(at: str, rmssd: float) -> dict[str, Any]:
    return {
        "heartRateVariability": {
            "rootMeanSquareOfSuccessiveDifferencesMilliseconds": rmssd,
            "sampleTime": {"physicalTime": at, "utcOffset": "7200s"},
        }
    }


ANSWERS: dict[str, list[dict[str, Any]]] = {
    "daily-heart-rate-variability": [
        {
            "dailyHeartRateVariability": {
                "averageHeartRateVariabilityMilliseconds": 47,
                "date": {"year": 2026, "month": 10, "day": 20},
            }
        }
    ],
    # One sample shares the daily value's instant (00:00 UTC of Google's date).
    "heart-rate-variability": [_sample("2026-10-20T00:00:00Z", 40), _sample("2026-10-20T03:00:00Z", 52)],
}


def _pull(db: Session, member_id: Any) -> None:
    data_247 = GoogleHealth247Data(oauth=MagicMock(), connection_repo=MagicMock(), api_base_url="https://health.test")

    def fake(*args: object, **kwargs: object) -> dict[str, Any]:
        data_type = str(kwargs.get("endpoint") or args[6]).split("/dataTypes/")[1].split("/")[0]
        return {"dataPoints": ANSWERS.get(data_type, [])}

    with (
        patch("app.services.providers.google_health.data_247.make_authenticated_request", side_effect=fake),
        patch.object(data_247.sleep, "load_and_save", return_value=0),
        patch.object(data_247.settings_repo, "get_data_granularity", return_value=None),
        patch("app.services.providers.google_health.data_247.store_raw_payload"),
    ):
        data_247.load_and_save_all(db, member_id, START, END)


def _stored(db: Session, member_id: Any) -> list[tuple[str, datetime, Decimal, str | None]]:
    rows = db.execute(
        select(
            SeriesTypeDefinition.code, DataPointSeries.recorded_at, DataPointSeries.value, DataPointSeries.zone_offset
        )
        .join(DataPointSeries, DataPointSeries.series_type_definition_id == SeriesTypeDefinition.id)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(DataSource.user_id == member_id)
        .order_by(SeriesTypeDefinition.code, DataPointSeries.recorded_at)
    )
    return [tuple(r) for r in rows]


@freeze_time("2026-10-21T12:00:00Z")
def test_the_daily_value_is_its_own_series_and_the_samples_stay_rmssd(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)

    _pull(db, member.id)

    assert _stored(db, member.id) == [
        ("google_hrv_rmssd_daily", MIDNIGHT, Decimal(47), None),
        ("heart_rate_variability_rmssd", MIDNIGHT, Decimal(40), "+02:00"),
        ("heart_rate_variability_rmssd", datetime(2026, 10, 20, 3, tzinfo=UTC), Decimal(52), "+02:00"),
    ]


@freeze_time("2026-10-21T12:00:00Z")
def test_the_rest_read_serves_the_daily_value(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    _pull(db, member.id)

    page = timeseries_service.get_timeseries(
        db,
        member.id,
        [DAILY],
        TimeSeriesQueryParams(start_datetime=datetime(2026, 10, 19, tzinfo=UTC), end_datetime=END),
    )

    assert [(s.type, s.value, s.source.provider if s.source else None) for s in page.data] == [
        (DAILY, 47, "google_health")
    ]


def test_the_series_has_id_599_in_milliseconds() -> None:
    assert (599, DAILY, "ms") in SERIES_TYPE_DEFINITIONS


def test_the_daily_series_is_announced_on_the_priority_lane() -> None:
    assert SERIES_TYPE_TO_GRANULAR_EVENT[DAILY.value] == EVENT
    assert EVENT in settings.webhook_priority_event_set
