"""2.96.6.1 (spec R1, AC2, AC4, AC17): which form of each data type a Polar pull fetches.

A type with a full form is fetched in full when it is due (never fetched in full, or last
fetched in full FULL_INTERVAL ago); otherwise sleep and daily activity are fetched in their
short form and the eight other range types not at all. A 429 ends the pull. Polar is faked at
``_make_api_request``; storage is real.
"""

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from freezegun import freeze_time
from sqlalchemy.orm import Session

from app.schemas.sync_status import DataTypeFailure
from app.services.providers.polar import data_247 as data_247_module
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.pull_budget import FULL_INTERVAL, RATE_LIMITED
from app.services.providers.polar.strategy import PolarStrategy
from app.services.providers.polar.workouts import PolarWorkouts
from tests.providers.polar.test_polar_no_lost_nights import TODAY, FakePolar, _polar_user, _stored_polar_nights

# The eight range types that have no short form, by the endpoint each one calls.
FULL_ONLY = {
    "continuous_hr": "/v3/users/continuous-heart-rate",
    "cardio_load": "/v3/users/cardio-load/date",
    "alertness": "/v3/users/sleepwise/alertness",
    "circadian_bedtime": "/v3/users/sleepwise/circadian-bedtime",
    "body_temperature": "/v3/users/body-temperature",
    "sleep_skin_temperature": "/v3/users/sleep-skin-temperature",
    "spo2": "/v3/users/spo2",
    "wrist_ecg": "/v3/users/wrist-ecg",
}
ACTIVITIES = "/v3/users/activities"


@pytest.fixture
def polar() -> FakePolar:
    return FakePolar()


@pytest.fixture
def data_247(polar: FakePolar) -> Iterator[Polar247Data]:
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)
    with patch.object(Polar247Data, "_make_api_request", side_effect=polar):
        yield data


def _after_full_interval() -> datetime:
    return datetime.now(timezone.utc) + FULL_INTERVAL + timedelta(minutes=1)


def _activity_from(polar: FakePolar) -> list[date]:
    return [date.fromisoformat(p["from"]) for p in polar.requested(ACTIVITIES)]


class TestTheFirstPull:
    @pytest.mark.parametrize(("data_type", "endpoint"), FULL_ONLY.items())
    def test_a_connection_never_pulled_fetches_each_type_in_full(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, data_type: str, endpoint: str
    ) -> None:
        user_id = _polar_user(db)

        result = data_247.load_and_save_all(db, user_id)

        assert data_type in result
        assert len(polar.requested(endpoint)) == 1

    def test_a_connection_never_pulled_fetches_daily_activity_for_90_days(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _polar_user(db)

        result = data_247.load_and_save_all(db, user_id)

        assert "daily_activity" in result
        assert min(_activity_from(polar)) == TODAY - timedelta(days=90)


class TestTheNextPull:
    """The pull after a full one, inside FULL_INTERVAL."""

    @pytest.fixture
    def user_id(self, db: Session, data_247: Polar247Data, polar: FakePolar) -> UUID:
        user_id = _polar_user(db)
        data_247.load_and_save_all(db, user_id)
        polar.calls.clear()
        return user_id

    @pytest.mark.parametrize(("data_type", "endpoint"), FULL_ONLY.items())
    def test_a_type_without_a_short_form_is_not_fetched(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, data_type: str, endpoint: str
    ) -> None:
        result = data_247.load_and_save_all(db, user_id)

        assert data_type not in result
        assert polar.requested(endpoint) == []

    def test_daily_activity_is_fetched_for_yesterday_and_today_in_one_call(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        result = data_247.load_and_save_all(db, user_id)

        assert "daily_activity_recent" in result
        assert "daily_activity" not in result
        (params,) = polar.requested(ACTIVITIES)
        assert date.fromisoformat(params["from"]) == TODAY - timedelta(days=1)
        assert date.fromisoformat(params["to"]) == TODAY

    def test_sleep_and_recharge_are_each_one_call(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        # AC11: Nightly Recharge is still requested on every pull.
        result = data_247.load_and_save_all(db, user_id)

        assert "sleep_latest" in result
        assert "sleep" not in result
        assert len(polar.requested("/v3/users/sleep/available")) == 1
        assert len(polar.requested("/v3/users/nightly-recharge")) == 1

    def test_the_whole_short_pull_is_three_calls_when_last_night_is_stored(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        data_247.load_and_save_all(db, user_id)

        assert sorted(e for e, _ in polar.calls) == sorted(
            ["/v3/users/sleep/available", "/v3/users/nightly-recharge", ACTIVITIES]
        )

    @pytest.mark.parametrize(("data_type", "endpoint"), FULL_ONLY.items())
    def test_each_type_is_fetched_in_full_again_once_the_interval_has_passed(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID, data_type: str, endpoint: str
    ) -> None:
        with freeze_time(_after_full_interval()):
            result = data_247.load_and_save_all(db, user_id)

        assert data_type in result
        assert len(polar.requested(endpoint)) == 1

    def test_daily_activity_is_fetched_for_90_days_again_once_the_interval_has_passed(
        self, db: Session, data_247: Polar247Data, polar: FakePolar, user_id: UUID
    ) -> None:
        with freeze_time(_after_full_interval()):
            result = data_247.load_and_save_all(db, user_id)

        assert "daily_activity" in result
        assert min(_activity_from(polar)) == (_after_full_interval() - timedelta(days=90)).date()

    def test_a_type_that_failed_in_full_is_fetched_in_full_on_the_next_pull(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # Spec cause 3, AC4: the next pull asks for it again.
        user_id = _polar_user(db)
        polar.failing_endpoints = {FULL_ONLY["spo2"]}
        data_247.load_and_save_all(db, user_id)
        polar.failing_endpoints = set()
        polar.calls.clear()

        result = data_247.load_and_save_all(db, user_id)

        assert result["spo2"] == 0
        assert len(polar.requested(FULL_ONLY["spo2"])) == 1
        assert polar.requested(FULL_ONLY["cardio_load"]) == []


class TestNights:
    def test_the_short_form_fetches_only_the_newest_night(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _polar_user(db)
        data_247.load_and_save_all(db, user_id)
        # Polar now lists last night, and a night from five days ago that reached it late.
        polar.nights = [TODAY, TODAY - timedelta(days=5)]
        polar.calls.clear()

        data_247.load_and_save_all(db, user_id)

        assert _stored_polar_nights(db, user_id) == {TODAY}

    def test_a_night_served_late_is_stored_by_the_next_full_pull(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # AC2: within 24 hours of being served. FULL_INTERVAL plus an hourly pull is under 24.
        assert FULL_INTERVAL + timedelta(hours=1) < timedelta(hours=24)
        user_id = _polar_user(db)
        data_247.load_and_save_all(db, user_id)
        polar.nights = [TODAY, TODAY - timedelta(days=5)]
        data_247.load_and_save_all(db, user_id)
        assert _stored_polar_nights(db, user_id) == {TODAY}

        with freeze_time(_after_full_interval()):
            data_247.load_and_save_all(db, user_id)

        assert _stored_polar_nights(db, user_id) == {TODAY, TODAY - timedelta(days=5)}

    def test_a_night_whose_short_fetch_failed_is_fetched_by_the_next_pull_once_no_longer_newest(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        # Spec cause 3, AC4: a failed fetch is asked for again on the next sync, even when a
        # newer night has appeared in between.
        user_id = _polar_user(db)
        data_247.load_and_save_all(db, user_id)
        yesterday = TODAY - timedelta(days=1)
        polar.nights = [yesterday]
        polar.failing_nights = {yesterday}
        assert isinstance(data_247.load_and_save_all(db, user_id)["sleep_latest"], DataTypeFailure)
        polar.failing_nights = set()
        polar.nights = [TODAY, yesterday]

        result = data_247.load_and_save_all(db, user_id)

        assert "sleep" in result
        assert _stored_polar_nights(db, user_id) == {TODAY, yesterday}


class TestRateLimit:
    def test_a_429_ends_the_pull_and_every_type_not_asked_reads_failed_with_its_code(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _polar_user(db)
        polar.rate_limited = True

        result = data_247.load_and_save_all(db, user_id)

        assert polar.calls == [("/v3/users/sleep/available", None)]
        assert len(result) == 11
        assert all(isinstance(v, DataTypeFailure) and v.code == RATE_LIMITED for v in result.values())

    def test_a_429_on_a_later_type_ends_the_pull_there(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _polar_user(db)
        polar.failing_endpoints = {"/v3/users/nightly-recharge"}
        polar.failure_status = 429

        result = data_247.load_and_save_all(db, user_id)

        assert [e for e, _ in polar.calls] == ["/v3/users/sleep/available", "/v3/users/nightly-recharge"]
        assert result["sleep"] == 0
        assert result["spo2"] == DataTypeFailure(code=RATE_LIMITED)

    def test_a_429_on_one_night_ends_the_nights(self, db: Session, data_247: Polar247Data, polar: FakePolar) -> None:
        user_id = _polar_user(db)
        polar.nights = [TODAY - timedelta(days=d) for d in range(1, 4)]
        polar.failing_nights = set(polar.nights)
        polar.failure_status = 429

        data_247.load_and_save_all(db, user_id)

        assert len([e for e, _ in polar.calls if e.startswith("/v3/users/sleep/2")]) == 1

    def test_what_a_429_left_unasked_is_fetched_in_full_once_polar_answers_again(
        self, db: Session, data_247: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _polar_user(db)
        polar.rate_limited = True
        data_247.load_and_save_all(db, user_id)
        polar.rate_limited = False
        polar.calls.clear()

        result = data_247.load_and_save_all(db, user_id)

        assert {"sleep", "daily_activity", *FULL_ONLY} <= result.keys()
        assert not any(isinstance(v, DataTypeFailure) for v in result.values())


class TestNoRetryOnRateLimit:
    """Retrying a 429 a few seconds later cannot clear Polar's windows and costs calls."""

    def test_the_247_requests_ask_for_no_retry(self, db: Session) -> None:
        data = Polar247Data(provider_name="polar", api_base_url="https://example.test", oauth=MagicMock())
        with patch.object(data_247_module, "make_authenticated_request", return_value={}) as request:
            data._make_api_request(db, UUID(int=1), "/v3/users/spo2")

        assert request.call_args.kwargs["retry_on_rate_limit"] is False

    def test_the_workout_requests_ask_for_no_retry(self, db: Session) -> None:
        workouts = PolarWorkouts(
            workout_repo=MagicMock(),
            connection_repo=MagicMock(),
            provider_name="polar",
            api_base_url="https://example.test",
            oauth=MagicMock(),
        )
        with patch(
            "app.services.providers.templates.base_workouts.make_authenticated_request", return_value=[]
        ) as request:
            workouts._make_api_request(db, UUID(int=1), "/v3/exercises")

        assert request.call_args.kwargs["retry_on_rate_limit"] is False
