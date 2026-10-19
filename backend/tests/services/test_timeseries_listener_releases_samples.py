"""A saved batch is released once its commit has fired the webhook listener.

``bulk_create_samples`` registers an ``after_commit`` listener with ``once=True``. SQLAlchemy keeps
a listener that has fired attached to the session as a no-op, so whatever its closure holds lives
as long as the session. A Polar pull runs every data type in one session: every sample it saved
stayed in memory until the pull ended (prod, 19 Oct 2026, up to 147k samples per member).
"""

import gc
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.schemas.enums import ProviderName, SeriesType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.timeseries_service import timeseries_service
from tests.factories import UserFactory

T0 = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
N = 300


class _SyncThread:
    """Runs the webhook thread's target inline."""

    def __init__(self, target: Any, args: tuple, daemon: bool) -> None:  # noqa: ARG002
        self._target, self._args = target, args

    def start(self) -> None:
        self._target(*self._args)


@pytest.fixture(params=[True, False], ids=["webhooks_on", "webhooks_off"])
def webhooks_on(request: pytest.FixtureRequest) -> bool:
    return request.param


@pytest.fixture
def emitted(webhooks_on: bool) -> Iterator[list[int]]:
    # Only the count of each emitted batch is kept, so the test itself holds no sample. A plain
    # function, not a Mock: a Mock records its call arguments and would keep the samples alive.
    counts: list[int] = []

    def emit(**kw: Any) -> None:
        counts.append(len(kw["samples"]))

    with (
        patch("app.services.timeseries_service.svix_service.is_enabled", return_value=webhooks_on),
        patch("app.services.timeseries_service.threading.Thread", _SyncThread),
        patch("app.services.timeseries_service.on_timeseries_batch_saved", new=emit),
    ):
        yield counts


def _live() -> int:
    gc.collect()
    return sum(1 for o in gc.get_objects() if type(o) is TimeSeriesSampleCreate)


def _save(db: Session, user_id: UUID) -> None:
    """Hand N samples to storage; nothing in the caller keeps them."""
    timeseries_service.bulk_create_samples(
        db,
        [
            TimeSeriesSampleCreate(
                id=uuid4(),
                user_id=user_id,
                provider=ProviderName.POLAR,
                source=ProviderName.POLAR,
                recorded_at=T0 + timedelta(minutes=i),
                value=60 + i % 40,
                series_type=SeriesType.heart_rate,
            )
            for i in range(N)
        ],
        emit_only_written=True,
    )


def test_a_committed_batch_is_not_kept_alive_by_its_listener(db: Session, emitted: list[int]) -> None:
    user = UserFactory()
    before = _live()

    _save(db, user.id)
    db.commit()

    assert _live() == before


def test_the_webhooks_still_carry_the_batch(db: Session, emitted: list[int], webhooks_on: bool) -> None:
    user = UserFactory()

    _save(db, user.id)
    db.commit()

    assert emitted == ([N] if webhooks_on else [])
