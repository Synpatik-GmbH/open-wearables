"""Test helpers for the Google Health history window (Calibra 2.72.2, design D-03)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.services.providers.google_health.history_window import HistoryWindow

# Far enough back that no test window starts before it.
OPEN_WINDOW = HistoryWindow(floor=date(2000, 1, 1), today=date(2000, 1, 2))


def google_source(db: Session, member_id: UUID, connected_at: datetime) -> None:
    """A Google Health history row whose freeze lies far ahead.

    Before the freeze the database gate admits every sample, so what a test stores does not
    depend on the day it runs: the gate compares the freeze with the database clock.
    """
    source = WearableHistoryRepository().begin_source(db, member_id, "google_health", connected_at)
    source.freeze_at = datetime(2100, 1, 1, tzinfo=UTC)
    db.flush()


@pytest.fixture
def open_window() -> Iterator[None]:
    """For tests that drive the fetch with a mocked session: no window can be read from it."""
    with patch("app.services.providers.google_health.data_247.history_window", return_value=OPEN_WINDOW):
        yield
