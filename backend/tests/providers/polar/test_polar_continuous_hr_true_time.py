"""2.96.8 (spec v1.1, AC1-AC8, P1-P3, P7): Polar continuous heart rate is stored at its true instant.

Polar sends a day of continuous heart rate as a bare local date plus ``HH:MM`` local wall times,
with no zone. Until 2.96.8 those were stored as if they were UTC, so every sample sat one UTC
offset late. The offset in force is read at ingest from Polar's sleep listing (each night's
start and end carry one) and from the ``zone_offset`` stored on Polar workouts (P1); nothing new
is stored (R-2026-10-21-c). Polar is faked at ``_make_api_request``; storage is real.
"""

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from freezegun import freeze_time
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, User
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.enums.series_types import get_series_type_id
from app.schemas.providers.polar import PolarWebhookEventType
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.pull_budget import WEBHOOK_HR_DAILY_CAP
from app.services.providers.polar.strategy import PolarStrategy
from tests.factories import DataSourceFactory, EventRecordFactory, UserConnectionFactory, UserFactory

TODAY = datetime.now(timezone.utc).date()
DAY = TODAY - timedelta(days=2)
CHR = "/v3/users/continuous-heart-rate"
LISTING = "/v3/users/sleep/available"


def _zone(hours: int) -> str:
    return f"{'+' if hours >= 0 else '-'}{abs(hours):02d}:00"


def _night(woke: date, hours: int, start: str = "23:00", end: str = "07:00") -> dict[str, str]:
    """A /sleep/available entry for the night that ended on ``woke``, at UTC offset ``hours``."""
    began = woke - timedelta(days=1) if start > end else woke
    return {
        "date": woke.isoformat(),
        "start_time": f"{began.isoformat()}T{start}:00{_zone(hours)}",
        "end_time": f"{woke.isoformat()}T{end}:00{_zone(hours)}",
    }


def _day(on: date, times: list[str], bpm: int = 60) -> dict[str, Any]:
    """Polar's continuous heart rate for one local date: bare date, local ``HH:MM`` samples."""
    return {
        "polar_user": "https://www.polaraccesslink.com/v3/users/123",
        "date": on.isoformat(),
        "heart_rate_samples": [{"heart_rate": bpm, "sample_time": t} for t in times],
    }


def _every_5_min(start: str, end: str) -> list[str]:
    at = datetime.combine(DAY, time.fromisoformat(start))
    stop = datetime.combine(DAY, time.fromisoformat(end))
    out = []
    while at <= stop:
        out.append(at.strftime("%H:%M"))
        at += timedelta(minutes=5)
    return out


class FakePolar:
    """Answers the sleep listing and continuous heart rate; everything else is empty."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.nights: list[dict[str, str]] = []
        self.heart_rates: list[dict[str, Any]] = []
        self.failing: set[str] = set()

    def __call__(
        self, db: Session, user_id: UUID, endpoint: str, params: dict | None = None, headers: dict | None = None
    ) -> Any:  # noqa: ARG002
        self.calls.append(endpoint)
        if any(endpoint.startswith(f) for f in self.failing):
            raise HTTPException(status_code=503, detail="down")
        if endpoint == LISTING:
            return {"available": list(self.nights)}
        if endpoint.startswith(CHR):
            return {"heart_rates": [dict(d) for d in self.heart_rates]}
        if endpoint.startswith("/v3/users/sleep/") or endpoint == "/v3/users/nightly-recharge":
            return {}
        return []


@pytest.fixture
def polar() -> FakePolar:
    return FakePolar()


@pytest.fixture
def data_247(polar: FakePolar) -> Any:
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)
    with patch.object(Polar247Data, "_make_api_request", side_effect=polar):
        yield data


@pytest.fixture
def user_id(db: Session) -> UUID:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="polar")
    db.commit()
    return user.id


def _pull(data_247: Polar247Data, db: Session, user_id: UUID) -> dict[str, Any]:
    return data_247.load_and_save_all(db, user_id)


def _webhook(data_247: Polar247Data, db: Session, user_id: UUID, on: date = DAY) -> dict[str, int]:
    return data_247.fetch_and_save_from_webhook(
        db, user_id, PolarWebhookEventType.CONTINUOUS_HEART_RATE, f"{CHR}/{on.isoformat()}"
    )


INGEST = {"pull": _pull, "webhook": _webhook}


def _stored(db: Session, user_id: UUID) -> list[datetime]:
    return [t for t, _ in _stored_values(db, user_id)]


def _stored_values(db: Session, user_id: UUID) -> list[tuple[datetime, int]]:
    rows = (
        db.query(DataPointSeries.recorded_at, DataPointSeries.value)
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            DataSource.provider == ProviderName.POLAR,
            DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.heart_rate),
        )
        .order_by(DataPointSeries.recorded_at)
        .all()
    )
    return [(r.recorded_at.astimezone(timezone.utc), int(r.value)) for r in rows]


def _utc(on: date, hhmm: str, minus_hours: int = 0) -> datetime:
    return datetime.combine(on, time.fromisoformat(hhmm), tzinfo=timezone.utc) - timedelta(hours=minus_hours)


def _logged(capsys: Any, action: str) -> list[dict[str, Any]]:
    """The structured log lines (JSON on stdout) carrying ``action``."""
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == action]


def _polar_workout(db: Session, user_id: UUID, local_start: datetime, hours: int, minutes: int = 60) -> None:
    source = DataSourceFactory(user=db.get(User, user_id), provider=ProviderName.POLAR, source="polar")
    start = local_start.replace(tzinfo=timezone.utc) - timedelta(hours=hours)
    EventRecordFactory(
        data_source=source,
        category="workout",
        start_datetime=start,
        end_datetime=start + timedelta(minutes=minutes),
        duration_seconds=minutes * 60,
        zone_offset=_zone(hours),
    )
    db.commit()


class TestTrueInstant:
    """AC1 (P1): local wall time T at offset O is stored at T minus O, on both ingest paths."""

    @pytest.mark.parametrize("path", INGEST)
    @pytest.mark.parametrize("hours", [0, 1, 2, 3, -5])
    def test_the_offset_comes_from_the_sleep_listing(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str, hours: int
    ) -> None:
        polar.nights = [_night(DAY, hours)]
        polar.heart_rates = [_day(DAY, ["12:00", "12:05"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", hours), _utc(DAY, "12:05", hours)]

    @pytest.mark.parametrize("path", INGEST)
    @pytest.mark.parametrize("hours", [0, 1, 2, 3, -5])
    def test_the_offset_comes_from_a_stored_polar_workout(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str, hours: int
    ) -> None:
        _polar_workout(db, user_id, datetime.combine(DAY, time(18, 0)), hours)
        polar.heart_rates = [_day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", hours)]

    @pytest.mark.parametrize("path", INGEST)
    def test_the_night_that_ends_on_a_date_sets_it_over_the_one_that_starts_on_it(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        # Travel: the night ending on DAY is at +2, the night starting on DAY at +3.
        polar.nights = [_night(DAY, 2), _night(DAY + timedelta(days=1), 3)]
        polar.heart_rates = [_day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", 2)]

    @pytest.mark.parametrize("path", INGEST)
    def test_a_nights_start_sets_the_date_it_starts_on(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        # The clocks-back night: it starts on DAY at +2 and ends on DAY + 1 at +1.
        woke = DAY + timedelta(days=1)
        polar.nights = [
            {
                "date": woke.isoformat(),
                "start_time": f"{DAY.isoformat()}T23:00:00+02:00",
                "end_time": f"{woke.isoformat()}T07:00:00+01:00",
            }
        ]
        polar.heart_rates = [_day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", 2)]

    @pytest.mark.parametrize("path", INGEST)
    def test_the_sleep_listing_is_read_before_the_workout(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        polar.nights = [_night(DAY, 2)]
        _polar_workout(db, user_id, datetime.combine(DAY, time(18, 0)), 3)
        polar.heart_rates = [_day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", 2)]

    @pytest.mark.parametrize("path", INGEST)
    def test_a_date_polar_sends_with_a_zone_is_converted_as_given(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        polar.nights = [_night(DAY, 3)]
        polar.heart_rates = [{**_day(DAY, ["12:00"]), "date": f"{DAY.isoformat()}T00:00:00+01:00"}]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", 1)]

    @pytest.mark.parametrize("path", INGEST)
    def test_nothing_new_is_stored_on_the_heart_rate_rows(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        # Spec invariant (R-2026-10-21-c): zone_offset on heart-rate rows stays empty.
        polar.nights = [_night(DAY, 2)]
        polar.heart_rates = [_day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        offsets = {r.zone_offset for r in db.query(DataPointSeries).all()}
        assert offsets == {None}


class TestTheNightAndTheWorkout:
    @pytest.mark.parametrize("path", INGEST)
    def test_no_sample_recorded_before_sleep_is_stored_inside_the_night(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        # AC2. Night 22:30 to 06:30 at +2; heart rate every 5 minutes from 21:00 to 08:00 local.
        # The value says when the sample was recorded, so it cannot be read back from where it
        # was stored: 90 before sleep, 50 asleep, 70 after.
        polar.nights = [_night(DAY, 2, start="22:30", end="06:30")]
        eve = DAY - timedelta(days=1)
        polar.heart_rates = [
            _day(eve, _every_5_min("21:00", "22:25"), bpm=90),
            _day(eve, _every_5_min("22:30", "23:55"), bpm=50),
            _day(DAY, _every_5_min("00:00", "06:30"), bpm=50),
            _day(DAY, _every_5_min("06:35", "08:00"), bpm=70),
        ]

        INGEST[path](data_247, db, user_id)

        start = datetime.combine(eve, time(20, 30), tzinfo=timezone.utc)
        end = datetime.combine(DAY, time(4, 30), tzinfo=timezone.utc)
        inside = [bpm for t, bpm in _stored_values(db, user_id) if start <= t <= end]
        assert inside == [50] * (8 * 12 + 1)

    @pytest.mark.parametrize("path", INGEST)
    def test_the_workout_window_reads_the_workouts_own_hour(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        # AC3. Workout 18:00 to 19:00 local at +1, stored at 17:00Z to 18:00Z.
        # 100 recorded in the hour before, 150 during, 80 after.
        _polar_workout(db, user_id, datetime.combine(DAY, time(18, 0)), 1)
        polar.heart_rates = [
            _day(DAY, _every_5_min("17:00", "17:55"), bpm=100),
            _day(DAY, _every_5_min("18:00", "19:00"), bpm=150),
            _day(DAY, _every_5_min("19:05", "19:30"), bpm=80),
        ]

        INGEST[path](data_247, db, user_id)

        window = [bpm for t, bpm in _stored_values(db, user_id) if _utc(DAY, "17:00") <= t <= _utc(DAY, "18:00")]
        assert window == [150] * 13


class TestOtherPolarTimesAreUnchanged:
    """AC4, AC5. AC6 is ``test_polar_workout_true_time.py``, unchanged by this item."""

    def test_daily_activity_keeps_polars_start_time(self, data_247: Polar247Data) -> None:
        raw = {"start_time": "2026-10-19T00:00:00", "end_time": "2026-10-19T23:59:59", "steps": 9000}

        (sample,) = [s for s in data_247.normalize_daily_activity([raw], UUID(int=1)) if s.value == 9000]

        assert sample.recorded_at == datetime(2026, 10, 19)

    def test_the_sleep_record_and_its_own_samples_keep_their_times(self, data_247: Polar247Data) -> None:
        raw = {
            "date": "2026-10-19",
            "sleep_start_time": "2026-10-18T23:00:00+02:00",
            "sleep_end_time": "2026-10-19T07:00:00+02:00",
            "hypnogram": {"23:00": 1, "23:30": 2, "06:55": 1},
            "heart_rate_samples": {"23:00": 58, "23:55": 52, "00:05": 50},
        }

        ((record, detail, _, hr),) = data_247.normalize_sleep([raw], UUID(int=1))

        zone = timezone(timedelta(hours=2))
        assert record.start_datetime == datetime(2026, 10, 18, 23, 0, tzinfo=zone)
        assert record.end_datetime == datetime(2026, 10, 19, 7, 0, tzinfo=zone)
        assert [s.recorded_at for s in hr] == [
            datetime(2026, 10, 18, 23, 0, tzinfo=zone),
            datetime(2026, 10, 18, 23, 55, tzinfo=zone),
            datetime(2026, 10, 19, 0, 5, tzinfo=zone),
        ]
        assert [(s.start_time, s.end_time) for s in detail.sleep_stages] == [
            (datetime(2026, 10, 18, 23, 0, tzinfo=zone), datetime(2026, 10, 18, 23, 30, tzinfo=zone)),
            (datetime(2026, 10, 18, 23, 30, tzinfo=zone), datetime(2026, 10, 19, 6, 55, tzinfo=zone)),
            (datetime(2026, 10, 19, 6, 55, tzinfo=zone), datetime(2026, 10, 19, 7, 0, tzinfo=zone)),
        ]


class TestReIngest:
    @pytest.mark.parametrize(("first", "second"), [("pull", "pull"), ("pull", "webhook"), ("webhook", "pull")])
    def test_the_same_payload_twice_adds_nothing(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, first: str, second: str
    ) -> None:
        # AC7 (P2), by any mix of the two paths.
        polar.nights = [_night(DAY, 2)]
        polar.heart_rates = [_day(DAY, _every_5_min("06:00", "09:00"))]

        INGEST[first](data_247, db, user_id)
        once = _stored(db, user_id)
        INGEST[second](data_247, db, user_id)

        assert _stored(db, user_id) == once


class TestNoOffsetSource:
    @pytest.mark.parametrize("path", INGEST)
    def test_the_nearest_offset_within_3_days_is_used(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        # P3: no night and no workout on DAY - 3; the night ending on DAY (3 days after) is at +3.
        far = DAY - timedelta(days=3)
        polar.nights = [_night(DAY, 3)]
        polar.heart_rates = [_day(far, ["12:00"])]

        INGEST[path](data_247, db, user_id, **({"on": far} if path == "webhook" else {}))

        assert _stored(db, user_id) == [_utc(far, "12:00", 3)]

    @pytest.mark.parametrize("path", INGEST)
    def test_a_workout_3_days_away_is_the_nearest_offset(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        _polar_workout(db, user_id, datetime.combine(DAY - timedelta(days=3), time(18, 0)), 3)
        polar.heart_rates = [_day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", 3)]

    @pytest.mark.parametrize("path", INGEST)
    @pytest.mark.parametrize(("days", "local_start", "hours"), [(3, time(22, 0), -5), (-3, time(0, 30), 14)])
    def test_a_workout_at_either_edge_of_its_local_day_3_days_away_is_found(
        self,
        db: Session,
        data_247: Polar247Data,
        polar: FakePolar,
        user_id: UUID,
        path: str,
        days: int,
        local_start: time,
        hours: int,
    ) -> None:
        # Codex on #45: DAY + 3 at 22:00 at -05:00 is stored on DAY + 4 (03:00Z), past a lookup that
        # ended at midnight UTC; DAY - 3 at 00:30 at +14:00 is stored on DAY - 4 (10:30Z).
        _polar_workout(db, user_id, datetime.combine(DAY + timedelta(days=days), local_start), hours)
        polar.heart_rates = [_day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", hours)]

    @pytest.mark.parametrize("path", INGEST)
    def test_the_earlier_of_two_equally_near_offsets_is_used(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        # DAY - 2 is set by a night ending on it (+1), DAY + 2 by a night starting on it (+3).
        polar.nights = [_night(DAY - timedelta(days=2), 1), _night(DAY + timedelta(days=3), 3)]
        polar.heart_rates = [_day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", 1)]

    @pytest.mark.parametrize("path", INGEST)
    def test_without_one_the_sample_is_stored_as_before_and_counted(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str, capsys: Any
    ) -> None:
        # P3, AC8: no source within 3 days. Stored uncorrected (as before 2.96.8), never dropped,
        # and the count reaches the log the build reports from. The night ending on DAY starts on
        # DAY - 1, four days after ``far``.
        far = DAY - timedelta(days=5)
        polar.nights = [_night(DAY, 2)]
        polar.heart_rates = [_day(far, ["12:00", "12:05"]), _day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(far, "12:00"), _utc(far, "12:05"), _utc(DAY, "12:00", 2)]
        (line,) = _logged(capsys, "polar_continuous_hr_uncorrected")
        assert line["count"] == 2

    @pytest.mark.parametrize("path", INGEST)
    def test_a_night_with_no_offset_does_not_set_one(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str
    ) -> None:
        # A listed night whose times carry no zone gives no offset; the workout's is used.
        polar.nights = [{"date": DAY.isoformat(), "start_time": f"{DAY}T00:30:00", "end_time": f"{DAY}T07:00:00"}]
        _polar_workout(db, user_id, datetime.combine(DAY, time(18, 0)), 3)
        polar.heart_rates = [_day(DAY, ["12:00"])]

        INGEST[path](data_247, db, user_id)

        assert _stored(db, user_id) == [_utc(DAY, "12:00", 3)]

    @pytest.mark.parametrize("path", INGEST)
    def test_no_sample_is_dropped(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, path: str, capsys: Any
    ) -> None:
        # AC8: stored count equals the payload's, across a corrected, a nearest and an uncorrected day.
        polar.nights = [_night(DAY, 2)]
        days = [DAY, DAY - timedelta(days=2), DAY - timedelta(days=9)]
        polar.heart_rates = [_day(d, _every_5_min("00:00", "23:55")) for d in days]

        INGEST[path](data_247, db, user_id)

        assert len(_stored(db, user_id)) == 3 * 288
        (line,) = _logged(capsys, "polar_continuous_hr_uncorrected")
        assert line["count"] == 288, "only DAY - 9 has no offset within 3 days"


class TestWebhookBudget:
    """P7: a continuous heart-rate webhook costs two calls (the day, the listing), so at most
    ``WEBHOOK_HR_DAILY_CAP`` of them call Polar in any 24 hours. Past the cap one calls nothing and
    stores nothing: the day is the next full fetch's (``_RANGE_WINDOW_DAYS``)."""

    def test_the_cap_holds_the_webhook_path_to_18_calls_a_day(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        polar.nights = [_night(DAY, 2)]
        polar.heart_rates = [_day(DAY, ["12:00"])]

        with freeze_time(datetime.combine(TODAY, time(9, 0), tzinfo=timezone.utc)):
            for _ in range(WEBHOOK_HR_DAILY_CAP + 5):
                _webhook(data_247, db, user_id)

        assert WEBHOOK_HR_DAILY_CAP * 2 == 18
        assert len(polar.calls) == 18

    def test_a_refused_webhook_stores_nothing_and_says_so(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, capsys: Any
    ) -> None:
        polar.nights = [_night(DAY, 2)]
        with freeze_time(datetime.combine(TODAY, time(9, 0), tzinfo=timezone.utc)):
            for _ in range(WEBHOOK_HR_DAILY_CAP):
                _webhook(data_247, db, user_id)
            polar.heart_rates = [_day(DAY, ["12:00"])]
            capsys.readouterr()
            saved = _webhook(data_247, db, user_id)

        assert saved == {"continuous_hr": 0}
        assert _stored(db, user_id) == []
        assert _logged(capsys, "polar_continuous_hr_webhook_deferred")

    def test_the_cap_rolls_over_24_hours_and_not_at_midnight(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        polar.nights = [_night(DAY, 2)]
        first = datetime.combine(TODAY, time(21, 30), tzinfo=timezone.utc)
        with freeze_time(first):
            for _ in range(WEBHOOK_HR_DAILY_CAP):
                _webhook(data_247, db, user_id)
        calls = len(polar.calls)
        # Past midnight in Berlin (summer or winter) and in UTC: still the same 24 hours.
        for at in (first + timedelta(hours=1), first + timedelta(hours=23, minutes=59)):
            with freeze_time(at):
                _webhook(data_247, db, user_id)
        assert len(polar.calls) == calls
        with freeze_time(first + timedelta(hours=24, seconds=1)):
            _webhook(data_247, db, user_id)

        assert len(polar.calls) == calls + 2

    def test_a_cap_that_cannot_be_read_calls_nothing(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        polar.nights = [_night(DAY, 2)]
        polar.heart_rates = [_day(DAY, ["12:00"])]
        with patch(
            "app.services.providers.polar.pull_budget.get_redis_client", side_effect=ConnectionError("redis down")
        ):
            saved = _webhook(data_247, db, user_id)

        assert saved == {"continuous_hr": 0}
        assert polar.calls == []

    def test_a_listing_that_fails_stores_nothing_from_the_webhook(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        # Stored without the night's offset, the samples would be stored again at their true time
        # by the next full fetch: two rows for one reading (AC7). The full fetch stores them once.
        polar.heart_rates = [_day(DAY, ["12:00"])]
        polar.failing = {LISTING}

        with pytest.raises(HTTPException, match="down"):
            _webhook(data_247, db, user_id)

        assert _stored(db, user_id) == []


class TestThePullAddsNoCall:
    def test_the_pull_reads_the_listing_once(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        # P7: the sleep task already reads the listing; continuous heart rate reuses that answer.
        polar.nights = [_night(DAY, 2)]
        polar.heart_rates = [_day(DAY, ["12:00"])]

        result = _pull(data_247, db, user_id)

        assert polar.calls.count(LISTING) == 1
        assert result["continuous_hr"] == 1

    def test_a_listing_that_failed_fails_continuous_heart_rate_without_a_second_call(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        polar.heart_rates = [_day(DAY, ["12:00"])]
        polar.failing = {LISTING}

        result = _pull(data_247, db, user_id)

        assert polar.calls.count(LISTING) == 1
        assert result["continuous_hr"].code
        assert _stored(db, user_id) == []
