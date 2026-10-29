"""FORK (Calibra 2.72.2, design D-03; spec 2.72 v0.2 section 2.3, P5, AC 1 and AC 2): the history window.

Nothing older than 28 days before the connect day (Europe/Berlin dates), and nothing before the
history floor, is stored; a request reaches at most one local day further back. The floor is the
first connection day less 28, which a reconnect does not move. Instant-filtered types are cut on
the same instants they are requested by; daily types and exercise are requested from one day
before their cut, and the client-side check decides what is stored.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, SeriesTypeDefinition
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import ProviderName
from app.services.providers.google_health.data_247 import GoogleHealth247Data
from app.services.providers.google_health.history_window import (
    HISTORY_DAYS,
    HistoryWindow,
    berlin_midnight,
    connect_pull_start,
    history_window,
)
from app.services.providers.google_health.sleep import GoogleHealthApiSleep
from app.services.providers.google_health.workouts import GoogleHealthApiWorkouts
from app.utils.wearable_history_clock import BERLIN, candidate_start
from app.utils.wearable_history_ingress import WearableHistoryMetadataUnavailableError
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.google_health.history import google_source

CONNECT_DAY = date(2026, 10, 29)
FLOOR = date(2026, 10, 1)  # CONNECT_DAY - 28
CONNECTED_AT = datetime(2026, 10, 29, 10, 0, tzinfo=BERLIN)
# The connect pull's window: 00:00 Berlin on c - 28 (CEST, +02:00) to the callback.
CONNECT_START = datetime(2026, 10, 1, 0, 0, tzinfo=BERLIN)
CONNECT_END = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def _d(days: int) -> date:
    return CONNECT_DAY + timedelta(days=days)


# -- the floor -----------------------------------------------------------------


def test_the_floor_is_the_history_rows_connection_day_less_28(db: Session) -> None:
    member = UserFactory()
    WearableHistoryRepository().begin_source(db, member.id, "google_health", CONNECTED_AT)
    db.flush()

    assert history_window(db, member.id, UserConnectionRepository()).floor == FLOOR


def test_a_reconnect_does_not_move_the_floor(db: Session) -> None:
    member = UserFactory()
    repository = WearableHistoryRepository()
    repository.begin_source(db, member.id, "google_health", CONNECTED_AT)
    repository.begin_source(db, member.id, "google_health", CONNECTED_AT + timedelta(days=22))
    db.flush()

    assert history_window(db, member.id, UserConnectionRepository()).floor == FLOOR


def test_without_a_history_row_the_floor_comes_from_the_connections_berlin_date(db: Session) -> None:
    member = UserFactory()
    # build, not create: the factory's create also begins a history row, dated today.
    # 23:30 UTC on 28 October is 00:30 on 29 October in Berlin.
    db.add(
        UserConnectionFactory.build(
            user_id=member.id, provider="google_health", created_at=datetime(2026, 10, 28, 23, 30, tzinfo=UTC)
        )
    )
    db.flush()
    assert WearableHistoryRepository().get(db, member.id, "google_health") is None

    assert history_window(db, member.id, UserConnectionRepository()).floor == FLOOR


def test_with_neither_the_window_is_unavailable_and_nothing_is_requested(db: Session) -> None:
    member = UserFactory()
    db.flush()

    with pytest.raises(WearableHistoryMetadataUnavailableError):
        history_window(db, member.id, UserConnectionRepository())


# -- the predicate ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "today", "floor", "keep"),
    [
        # The connect pull, on the connect day and after Berlin midnight: the look-back day is not kept.
        (CONNECT_START, CONNECT_DAY, FLOOR, FLOOR),
        (CONNECT_START, _d(1), FLOOR, FLOOR),
        # A reconnect on 20 November: s = 23 October, floor unchanged.
        (datetime(2026, 10, 23, tzinfo=BERLIN), date(2026, 11, 20), FLOOR, date(2026, 10, 23)),
        # A live pull keeps its look-back day.
        (datetime(2026, 11, 21, 7, tzinfo=UTC), date(2026, 11, 21), FLOOR, date(2026, 11, 20)),
        # A retry over a held cursor more than 27 days back keeps from s, losing only the look-back day.
        (datetime(2026, 10, 5, 6, tzinfo=UTC), date(2026, 11, 20), FLOOR, date(2026, 10, 5)),
        # A window that starts before the floor is cut at the floor.
        (datetime(2026, 9, 20, tzinfo=UTC), _d(1), FLOOR, FLOOR),
        # The boundary: s - 1 exactly t - 28 keeps it.
        (datetime(2026, 10, 24, 6, tzinfo=UTC), date(2026, 11, 20), FLOOR, date(2026, 10, 23)),
    ],
)
def test_the_daily_store_cut(start: datetime, today: date, floor: date, keep: date) -> None:
    assert HistoryWindow(floor=floor, today=today).keep_date(start) == keep


@pytest.mark.parametrize(
    ("start", "request_date"),
    [
        (CONNECT_START, date(2026, 9, 30)),
        (datetime(2026, 9, 20, tzinfo=UTC), date(2026, 9, 30)),
        (datetime(2026, 11, 21, 7, tzinfo=UTC), date(2026, 11, 20)),
    ],
)
def test_a_date_request_asks_from_one_day_before_its_lower_bound(start: datetime, request_date: date) -> None:
    assert HistoryWindow(floor=FLOOR, today=CONNECT_DAY).request_date(start) == request_date


def test_the_instant_cut_is_the_later_of_the_window_start_and_berlin_midnight_on_the_floor() -> None:
    window = HistoryWindow(floor=FLOOR, today=CONNECT_DAY)
    assert window.instant_cut(datetime(2026, 9, 20, tzinfo=UTC)) == berlin_midnight(FLOOR)
    assert window.instant_cut(datetime(2026, 10, 2, 5, tzinfo=UTC)) == datetime(2026, 10, 2, 5, tzinfo=UTC)


# -- the connect pull -----------------------------------------------------------


def test_the_connect_pull_starts_at_berlin_midnight_28_days_before_the_connect_day() -> None:
    assert connect_pull_start(datetime(2026, 10, 29, 8, tzinfo=UTC)) == CONNECT_START
    # 23:30 UTC on 28 October is the 29th in Berlin.
    assert connect_pull_start(datetime(2026, 10, 28, 23, 30, tzinfo=UTC)) == CONNECT_START


def _connect(client: TestClient, db: Session, provider: str) -> Any:
    strategy = MagicMock()
    strategy.capabilities.webhook_callback = False
    strategy.capabilities.rest_pull = True
    member = UserFactory()
    db.add(UserConnectionFactory.build(user_id=member.id, provider=provider))
    db.flush()
    strategy.oauth.handle_callback.return_value = MagicMock(user_id=member.id, redirect_uri=None)
    with (
        freeze_time("2026-10-29T08:00:00Z"),
        patch("app.api.routes.v1.oauth.get_oauth_strategy", return_value=strategy),
        patch("app.api.routes.v1.oauth.user_connection_service.stamp_last_synced_at"),
        patch("app.integrations.celery.tasks.sync_vendor_data") as task,
    ):
        response = client.get(
            f"/api/v1/oauth/{provider}/callback", params={"code": "c", "state": "s"}, follow_redirects=False
        )
    assert response.status_code == 303
    return task.delay.call_args.kwargs


def test_the_google_connect_pull_requests_28_days(client: TestClient, db: Session) -> None:
    assert datetime.fromisoformat(_connect(client, db, "google_health")["start_date"]) == CONNECT_START


# Every provider but Google Health: the route's rule names one provider, so the set is derived.
OTHER_PROVIDERS = sorted(
    p.value for p in ProviderName if p not in (ProviderName.GOOGLE_HEALTH, ProviderName.UNKNOWN, ProviderName.INTERNAL)
)


@pytest.mark.parametrize("provider", OTHER_PROVIDERS)
def test_every_other_providers_connect_pull_keeps_90_days(client: TestClient, db: Session, provider: str) -> None:
    start = datetime.fromisoformat(_connect(client, db, provider)["start_date"])
    assert start == datetime(2026, 10, 29, 8, tzinfo=UTC) - timedelta(days=90)


# -- what is requested and stored ------------------------------------------------

_SAMPLE_TIME = {"utcOffset": "7200s"}


def _points(data_type: str) -> list[dict[str, Any]]:
    if data_type == "daily-heart-rate-variability":
        return [
            {
                "dailyHeartRateVariability": {
                    "averageHeartRateVariabilityMilliseconds": v,
                    "date": {"year": 2026, "month": m, "day": d},
                }
            }
            for (m, d, v) in [(9, 30, 41), (10, 1, 47)]
        ]
    if data_type == "heart-rate":
        return [
            {"heartRate": {"beatsPerMinute": v, "sampleTime": {"physicalTime": t, **_SAMPLE_TIME}}}
            # 21:59 UTC on 30 September is 23:59 in Berlin, before the cut; 22:01 UTC is 00:01 on 1 October.
            for (t, v) in [("2026-09-30T21:59:00Z", 61), ("2026-09-30T22:01:00Z", 62)]
        ]
    return []


class _FakeGoogle:
    def __init__(self) -> None:
        self.filters: dict[str, str] = {}

    def __call__(self, *args: object, **kwargs: object) -> dict[str, Any]:
        endpoint = str(kwargs.get("endpoint") or args[6])
        data_type = endpoint.split("/dataTypes/")[1].split("/")[0]
        self.filters[data_type] = (kwargs.get("params") or {}).get("filter", "")
        return {"dataPoints": _points(data_type)}


def _stored(db: Session, member_id: Any) -> list[tuple[str, datetime]]:
    rows = db.execute(
        select(SeriesTypeDefinition.code, DataPointSeries.recorded_at)
        .join(DataPointSeries, DataPointSeries.series_type_definition_id == SeriesTypeDefinition.id)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(DataSource.user_id == member_id)
        .order_by(SeriesTypeDefinition.code, DataPointSeries.recorded_at)
    )
    return [tuple(r) for r in rows]


@freeze_time("2026-10-29T09:00:00Z")
def test_the_connect_pull_stores_nothing_before_the_window_and_asks_one_day_further_at_most(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    data_247 = GoogleHealth247Data(
        oauth=MagicMock(), connection_repo=UserConnectionRepository(), api_base_url="https://h.test"
    )
    fake = _FakeGoogle()
    with (
        patch("app.services.providers.google_health.data_247.make_authenticated_request", side_effect=fake),
        patch.object(data_247.sleep, "load_and_save", return_value=0),
        patch.object(data_247.settings_repo, "get_data_granularity", return_value=None),
        patch("app.services.providers.google_health.data_247.store_raw_payload"),
    ):
        data_247.load_and_save_all(db, member.id, CONNECT_START, CONNECT_END)

    # AC 1: nothing dated or sampled before 1 October is stored.
    assert _stored(db, member.id) == [
        ("google_hrv_rmssd_daily", datetime(2026, 10, 1, tzinfo=UTC)),
        ("heart_rate", datetime(2026, 9, 30, 22, 1, tzinfo=UTC)),
    ]
    # AC 1: no request reaches more than one local day before c - 28.
    assert 'daily_heart_rate_variability.date >= "2026-09-30"' in fake.filters["daily-heart-rate-variability"]
    assert 'heart_rate.sample_time.physical_time >= "2026-09-30T22:00:00Z"' in fake.filters["heart-rate"]


def test_a_window_wholly_before_the_floor_requests_nothing(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    data_247 = GoogleHealth247Data(
        oauth=MagicMock(), connection_repo=UserConnectionRepository(), api_base_url="https://h.test"
    )
    fake = _FakeGoogle()
    with patch("app.services.providers.google_health.data_247.make_authenticated_request", side_effect=fake):
        assert (
            data_247.sync_data_type(
                db, member.id, "heart-rate", datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
            )
            is None
        )
    assert fake.filters == {}


def _exercise(start_utc: str, civil_date: tuple[int, int, int]) -> dict[str, Any]:
    y, m, d = civil_date
    return {
        "name": f"users/me/dataTypes/exercise/dataPoints/{start_utc}",
        "exercise": {
            "exerciseType": "RUNNING",
            "interval": {
                "startTime": start_utc,
                "endTime": start_utc.replace("T01", "T02").replace("T21", "T22"),
                "startUtcOffset": "-14400s",
                "civilStartTime": {"date": {"year": y, "month": m, "day": d}},
            },
        },
    }


@freeze_time("2026-10-29T09:00:00Z")
def test_an_in_window_workout_of_a_member_west_of_utc_is_stored(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    workouts = GoogleHealthApiWorkouts(MagicMock(), UserConnectionRepository(), MagicMock(), "https://h.test")
    # 21:00 in New York on 30 September is 01:00 UTC on 1 October, 03:00 in Berlin: inside the window.
    inside = _exercise("2026-10-01T01:00:00Z", (2026, 9, 30))
    # 17:00 in New York on 30 September is 21:00 UTC, 23:00 in Berlin on 30 September: before it.
    before = _exercise("2026-09-30T21:00:00Z", (2026, 9, 30))
    asked: list[dict[str, Any]] = []

    def fake(_db: Any, _user: Any, _endpoint: str, method: str = "GET", params: dict | None = None) -> dict:
        asked.append(params or {})
        return {"dataPoints": [inside, before]}

    with (
        patch.object(workouts, "_make_api_request", side_effect=fake),
        patch("app.services.providers.google_health.workouts.store_raw_payload"),
    ):
        kept = workouts.get_workouts(db, member.id, CONNECT_START, CONNECT_END)

    assert [p["name"] for p in kept] == [inside["name"]]
    assert asked
    assert all(p.get("filter") == 'exercise.interval.civil_start_time >= "2026-09-30"' for p in asked)


def _sleep(start_utc: str, end_utc: str) -> dict[str, Any]:
    return {
        "name": f"users/me/dataTypes/sleep/dataPoints/{end_utc}",
        "sleep": {
            "interval": {"startTime": start_utc, "endTime": end_utc, "startUtcOffset": "7200s", "endUtcOffset": "7200s"}
        },
    }


@freeze_time("2026-10-29T09:00:00Z")
def test_the_first_night_stored_is_the_one_ending_on_the_first_day(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    sleep = GoogleHealthApiSleep(MagicMock(), UserConnectionRepository(), "https://h.test")
    # 23:00 Berlin on 30 September to 07:00 on 1 October: night 1 October, ends inside the window.
    first = _sleep("2026-09-30T21:00:00Z", "2026-10-01T05:00:00Z")
    # A nap ending 23:30 Berlin on 30 September: before the cut.
    before = _sleep("2026-09-30T20:00:00Z", "2026-09-30T21:30:00Z")
    asked: list[str] = []
    offered: list[datetime] = []

    def fake(**kwargs: Any) -> dict[str, Any]:
        asked.append(kwargs["params"]["filter"])
        return {"dataPoints": [first, before]}

    def merge(_db: Any, _user: Any, record: Any, _detail: Any, _gap: Any) -> Any:
        offered.append(record.end_datetime)
        return record

    with (
        patch("app.services.providers.google_health.sleep.make_authenticated_request", side_effect=fake),
        patch("app.services.providers.google_health.sleep.store_raw_payload"),
        patch(
            "app.services.providers.google_health.sleep.event_record_service.create_or_merge_sleep", side_effect=merge
        ),
    ):
        sleep.load_and_save(db, member.id, datetime(2026, 9, 25, tzinfo=UTC), CONNECT_END)

    assert asked == [
        'sleep.interval.end_time >= "2026-09-30T22:00:00Z" AND sleep.interval.end_time < "2026-10-29T09:00:00Z"'
    ]
    assert offered == [datetime(2026, 10, 1, 5, tzinfo=UTC)]


# -- the empty range, per request kind (design D-03) ------------------------------

_BEFORE_FLOOR = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC))


@pytest.mark.parametrize("data_type", ["heart-rate", "daily-heart-rate-variability"])
def test_a_window_wholly_before_the_floor_requests_nothing_for_any_24_7_shape(db: Session, data_type: str) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    data_247 = GoogleHealth247Data(
        oauth=MagicMock(), connection_repo=UserConnectionRepository(), api_base_url="https://h.test"
    )
    fake = _FakeGoogle()
    with patch("app.services.providers.google_health.data_247.make_authenticated_request", side_effect=fake):
        assert data_247.sync_data_type(db, member.id, data_type, *_BEFORE_FLOOR) is None
    assert fake.filters == {}


def test_a_sleep_window_wholly_before_the_floor_requests_nothing(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    sleep = GoogleHealthApiSleep(MagicMock(), UserConnectionRepository(), "https://h.test")
    with patch("app.services.providers.google_health.sleep.make_authenticated_request") as request:
        assert sleep.load_and_save(db, member.id, *_BEFORE_FLOOR) == 0
    request.assert_not_called()


def test_an_exercise_window_wholly_before_the_floor_requests_nothing(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    workouts = GoogleHealthApiWorkouts(MagicMock(), UserConnectionRepository(), MagicMock(), "https://h.test")
    with patch.object(workouts, "_make_api_request") as request:
        assert workouts.get_workouts(db, member.id, *_BEFORE_FLOOR) == []
    request.assert_not_called()


def test_the_floor_is_the_earliest_start_date_wearable_history_can_choose() -> None:
    """The 28 here and the 28 in 2.96.9's candidate start must stay the same number."""
    day = CONNECT_DAY
    every_night = [day - timedelta(days=i) for i in range(40)]
    assert candidate_start(day, every_night) == HistoryWindow(floor=day - timedelta(days=HISTORY_DAYS), today=day).floor


# -- the cuts on a window that starts before the floor, and the boundary --------------


def test_an_instant_point_before_the_floor_fetched_by_an_earlier_window_is_not_stored(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    data_247 = GoogleHealth247Data(
        oauth=MagicMock(), connection_repo=UserConnectionRepository(), api_base_url="https://h.test"
    )
    with patch("app.services.providers.google_health.data_247.make_authenticated_request", side_effect=_FakeGoogle()):
        data_247.sync_data_type(db, member.id, "heart-rate", datetime(2026, 9, 25, tzinfo=UTC), CONNECT_END)

    # 21:59 UTC on 30 September is before 00:00 Berlin on 1 October; 22:01 UTC is after it.
    assert _stored(db, member.id) == [("heart_rate", datetime(2026, 9, 30, 22, 1, tzinfo=UTC))]


@freeze_time("2026-10-29T09:00:00Z")
def test_an_exercise_before_the_floor_fetched_by_an_earlier_window_is_not_kept(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    workouts = GoogleHealthApiWorkouts(MagicMock(), UserConnectionRepository(), MagicMock(), "https://h.test")
    # 23:00 Berlin on 30 September, before the floor; 03:00 Berlin on 1 October, after it.
    before = _exercise("2026-09-30T21:00:00Z", (2026, 9, 30))
    inside = _exercise("2026-10-01T01:00:00Z", (2026, 9, 30))
    with (
        patch.object(workouts, "_make_api_request", return_value={"dataPoints": [before, inside]}),
        patch("app.services.providers.google_health.workouts.store_raw_payload"),
    ):
        kept = workouts.get_workouts(db, member.id, datetime(2026, 9, 25, tzinfo=UTC), CONNECT_END)

    assert [p["name"] for p in kept] == [inside["name"]]


def test_a_window_ending_exactly_at_the_cut_requests_nothing(db: Session) -> None:
    member = UserFactory()
    google_source(db, member.id, CONNECTED_AT)
    window = (datetime(2026, 9, 20, tzinfo=UTC), berlin_midnight(FLOOR))
    data_247 = GoogleHealth247Data(
        oauth=MagicMock(), connection_repo=UserConnectionRepository(), api_base_url="https://h.test"
    )
    sleep = GoogleHealthApiSleep(MagicMock(), UserConnectionRepository(), "https://h.test")
    workouts = GoogleHealthApiWorkouts(MagicMock(), UserConnectionRepository(), MagicMock(), "https://h.test")
    fake = _FakeGoogle()
    with (
        patch("app.services.providers.google_health.data_247.make_authenticated_request", side_effect=fake),
        patch("app.services.providers.google_health.sleep.make_authenticated_request") as sleep_request,
        patch.object(workouts, "_make_api_request") as exercise_request,
    ):
        assert data_247.sync_data_type(db, member.id, "heart-rate", *window) is None
        assert sleep.load_and_save(db, member.id, *window) == 0
        assert workouts.get_workouts(db, member.id, *window) == []
    assert fake.filters == {}
    sleep_request.assert_not_called()
    exercise_request.assert_not_called()
