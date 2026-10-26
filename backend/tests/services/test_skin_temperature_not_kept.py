"""FORK (Calibra ruling R-2026-10-23-f, on DPO point 9): skin temperature is not kept.

No skin-temperature series, from any provider, is stored or announced: not the night's value,
not its deviation from the member's baseline, not its trend deviation, not Garmin's. Every
write path into ``data_point_series`` drops them, and a batch that also carries other series
keeps the rest.
"""

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants.series_types.not_kept import NOT_KEPT_SERIES
from app.models import DataPointSeries, DataSource, SeriesTypeDefinition
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.timeseries_service import timeseries_service
from tests.factories import UserFactory

T0 = datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)

SKIN = [
    SeriesType.skin_temperature,
    SeriesType.skin_temperature_deviation,
    SeriesType.skin_temperature_trend_deviation,
    SeriesType.garmin_skin_temperature,
]


def _sample(user_id: Any, series: SeriesType, value: float) -> TimeSeriesSampleCreate:
    return TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=user_id,
        provider=ProviderName.POLAR,
        source=ProviderName.POLAR,
        recorded_at=T0,
        value=value,
        series_type=series,
    )


def _stored(db: Session, user_id: Any) -> list[str]:
    rows = db.execute(
        select(SeriesTypeDefinition.code)
        .join(DataPointSeries, DataPointSeries.series_type_definition_id == SeriesTypeDefinition.id)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(DataSource.user_id == user_id)
    )
    return sorted(code for (code,) in rows)


class _SyncThread:
    """Runs the webhook thread's target inline, so the test sees what it emitted."""

    def __init__(self, target: Any, args: tuple, daemon: bool) -> None:  # noqa: ARG002
        self._target, self._args = target, args

    def start(self) -> None:
        self._target(*self._args)


@pytest.fixture
def emitted() -> Iterator[list[dict]]:
    calls: list[dict] = []
    with (
        patch("app.services.timeseries_service.svix_service.is_enabled", return_value=True),
        patch("app.services.timeseries_service.threading.Thread", _SyncThread),
        patch("app.services.timeseries_service.on_timeseries_batch_saved", side_effect=lambda **kw: calls.append(kw)),
    ):
        yield calls


def test_the_not_kept_set_is_exactly_the_skin_temperature_series() -> None:
    assert frozenset(SKIN) == NOT_KEPT_SERIES
    assert {s for s in SeriesType if "skin_temp" in s.value} == set(SKIN)


@pytest.mark.parametrize("series", SKIN)
def test_bulk_create_stores_no_skin_temperature_and_keeps_the_rest(db: Session, series: SeriesType) -> None:
    user = UserFactory()
    repo = DataPointSeriesRepository(DataPointSeries)

    counts = repo.bulk_create(db, [_sample(user.id, series, 33.6), _sample(user.id, SeriesType.resting_heart_rate, 52)])

    assert (counts.inserted, counts.updated) == (1, 0)
    assert _stored(db, user.id) == ["resting_heart_rate"]


@pytest.mark.parametrize("series", SKIN)
def test_create_stores_no_skin_temperature(db: Session, series: SeriesType) -> None:
    user = UserFactory()
    repo = DataPointSeriesRepository(DataPointSeries)

    assert repo.create(db, _sample(user.id, series, 33.6)) is None
    assert _stored(db, user.id) == []


@pytest.mark.parametrize("series", SKIN)
def test_the_service_neither_stores_nor_announces_skin_temperature(
    db: Session, emitted: list[dict], series: SeriesType
) -> None:
    user = UserFactory()

    timeseries_service.bulk_create_samples(
        db, [_sample(user.id, series, 33.6), _sample(user.id, SeriesType.resting_heart_rate, 52)]
    )
    db.commit()

    assert _stored(db, user.id) == ["resting_heart_rate"]
    assert [s["type"] for call in emitted for s in call["samples"]] == ["resting_heart_rate"]
