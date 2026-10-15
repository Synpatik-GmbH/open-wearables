"""2.96.6.1: a caller can ask for webhooks only for the samples a save actually wrote.

Polar's hourly pull now re-reads the whole window Polar serves (up to 90 days), so most of
every batch is already stored. Unchanged rows must not go out again as webhooks. The default
(every sample handed in is emitted) is unchanged for every other caller.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.schemas.enums import ProviderName, SeriesType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.timeseries_service import timeseries_service
from tests.factories import UserFactory

T0 = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)


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


def _hrv(user_id: Any, day: int, value: float) -> TimeSeriesSampleCreate:
    return TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=user_id,
        provider=ProviderName.POLAR,
        source=ProviderName.POLAR,
        recorded_at=T0 + timedelta(days=day),
        value=value,
        series_type=SeriesType.heart_rate_variability_rmssd,
    )


def _emitted_values(calls: list[dict]) -> list[float]:
    return [s["value"] for call in calls for s in call["samples"]]


def test_only_new_or_changed_samples_are_emitted_when_asked(db: Session, emitted: list[dict]) -> None:
    user = UserFactory()
    timeseries_service.bulk_create_samples(db, [_hrv(user.id, 0, 40.0), _hrv(user.id, 1, 41.0)], emit_only_written=True)
    db.commit()
    emitted.clear()

    # Day 0 unchanged, day 1 changed, day 2 new.
    timeseries_service.bulk_create_samples(
        db, [_hrv(user.id, 0, 40.0), _hrv(user.id, 1, 45.0), _hrv(user.id, 2, 42.0)], emit_only_written=True
    )
    db.commit()

    assert sorted(_emitted_values(emitted)) == [42.0, 45.0]


def test_a_batch_with_nothing_new_emits_nothing_when_asked(db: Session, emitted: list[dict]) -> None:
    user = UserFactory()
    batch = [_hrv(user.id, 0, 40.0)]
    timeseries_service.bulk_create_samples(db, batch, emit_only_written=True)
    db.commit()
    emitted.clear()

    timeseries_service.bulk_create_samples(db, [_hrv(user.id, 0, 40.0)], emit_only_written=True)
    db.commit()

    assert emitted == []


def test_the_default_still_emits_every_sample(db: Session, emitted: list[dict]) -> None:
    user = UserFactory()
    timeseries_service.bulk_create_samples(db, [_hrv(user.id, 0, 40.0)])
    db.commit()
    emitted.clear()

    timeseries_service.bulk_create_samples(db, [_hrv(user.id, 0, 40.0)])
    db.commit()

    assert _emitted_values(emitted) == [40.0]


def test_a_sample_with_a_naive_timestamp_is_matched_to_its_written_row(db: Session, emitted: list[dict]) -> None:
    # Polar's continuous heart rate is still built with naive local timestamps (spec non-goal,
    # 2.96.8). The written-row match must not depend on Python comparing naive and aware values.
    user = UserFactory()
    naive = _hrv(user.id, 0, 50.0).model_copy(update={"recorded_at": datetime(2026, 10, 1, 4, 0)})

    timeseries_service.bulk_create_samples(db, [naive], emit_only_written=True)
    db.commit()

    assert _emitted_values(emitted) == [50.0]
