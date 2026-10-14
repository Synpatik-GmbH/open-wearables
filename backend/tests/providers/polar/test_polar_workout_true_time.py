"""2.96.6.1 (spec N9, AC12): a Polar exercise is stored at its true start instant.

Polar sends ``start_time`` as local wall-clock time with no zone, plus
``start_time_utc_offset`` in minutes. The true UTC instant is local minus offset.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import EventRecord
from app.services.providers.polar.strategy import PolarStrategy
from tests.factories import UserConnectionFactory, UserFactory


def _exercise(exercise_id: str, start_time: str, offset_minutes: int, duration: str = "PT1H0M0S") -> dict:
    return {
        "id": exercise_id,
        "upload_time": "2026-10-13T09:00:00.000Z",
        "polar_user": "https://www.polaraccesslink.com/v3/users/12345",
        "device": "Polar Vantage V2",
        "start_time": start_time,
        "start_time_utc_offset": offset_minutes,
        "duration": duration,
        "sport": "RUNNING",
        "detailed_sport_info": "RUNNING",
    }


class TestPolarStartInstant:
    def test_berlin_summer_1025_is_0825_utc(self) -> None:
        workouts = PolarStrategy().workouts

        start, end = workouts._extract_dates_with_offset("2026-10-13T10:25:00", 120, "PT45M0S")

        assert start == datetime(2026, 10, 13, 8, 25, tzinfo=timezone.utc)
        assert end == datetime(2026, 10, 13, 9, 10, tzinfo=timezone.utc)

    def test_negative_offset_is_added_back(self) -> None:
        workouts = PolarStrategy().workouts

        start, _ = workouts._extract_dates_with_offset("2026-01-15T08:00:00", -300, "PT30M0S")

        assert start == datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)

    def test_a_start_time_that_already_carries_a_zone_is_not_shifted_again(self) -> None:
        workouts = PolarStrategy().workouts

        start, _ = workouts._extract_dates_with_offset("2026-10-13T10:25:00+02:00", 120, "PT45M0S")

        assert start == datetime(2026, 10, 13, 8, 25, tzinfo=timezone.utc)

    @patch("app.services.providers.templates.base_workouts.make_authenticated_request")
    def test_stored_row_holds_the_true_instant_whatever_the_session_time_zone(
        self, mock_request: MagicMock, db: Session
    ) -> None:
        # A naive datetime bound to timestamptz is read in the SESSION time zone, so the stored
        # instant must not depend on it. Berlin here would move a naive 08:25 to 06:25Z.
        db.execute(text("SET TIME ZONE 'Europe/Berlin'"))
        user = UserFactory()
        UserConnectionFactory(user=user, provider="polar")
        mock_request.return_value = [_exercise("EX-1025", "2026-10-13T10:25:00", 120, "PT45M0S")]

        PolarStrategy().workouts.load_data(db, user.id)

        row = db.query(EventRecord).filter(EventRecord.external_id == "EX-1025").one()
        assert row.start_datetime == datetime(2026, 10, 13, 8, 25, tzinfo=timezone.utc)
        assert row.end_datetime == datetime(2026, 10, 13, 9, 10, tzinfo=timezone.utc)
        assert row.zone_offset == "+02:00"
