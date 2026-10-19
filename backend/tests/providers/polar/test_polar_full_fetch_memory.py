"""A Polar full fetch holds one batch of samples at a time, and a killed one is not retried at once.

Prod, 15-19 Oct 2026: a full fetch re-reads what Polar serves (spec 2.96.6.1, causes 1-3 and 8),
up to 147k samples for one member. Building every sample of a type before saving any held them all
in the I/O worker at once, for every Polar member pulled together, and the kernel killed the worker
(status 137 at 1 GiB). The killed fetch recorded nothing, so the next hourly pull fetched it again
and was killed again.

Polar is faked at ``_make_api_request``; storage is real.
"""

import gc
import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from freezegun import freeze_time
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, PolarPullState
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.schemas.sync_status import DataTypeFailure
from app.services.providers.polar import data_247 as data_247_module
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.pull_budget import RETRY_FIRST_GAP, full_due
from app.services.providers.polar.strategy import PolarStrategy
from app.services.timeseries_service import timeseries_service
from tests.providers.polar.test_polar_no_lost_nights import TODAY, FakePolar, _polar_user

CONTINUOUS_HR = "/v3/users/continuous-heart-rate"
BODY_TEMPERATURE = "/v3/users/body-temperature"
SKIN_TEMPERATURE = "/v3/users/sleep-skin-temperature"
SPO2 = "/v3/users/spo2"
WRIST_ECG = "/v3/users/wrist-ecg"
ACTIVITIES = "/v3/users/activities"

BATCH = 500
# Polar serves 28 days of continuous heart rate and body temperature.
DAYS = 28
PER_DAY = 400


def _hhmmss(second: int) -> str:
    return f"{second // 3600:02d}:{second % 3600 // 60:02d}:{second % 60:02d}"


class DensePolar(FakePolar):
    """FakePolar that serves DAYS records for every series type a full fetch saves; the dense two
    (continuous heart rate, body temperature) carry PER_DAY samples a record. ``serve`` limits
    which endpoints answer with data; the rest answer as FakePolar does (nothing)."""

    def __init__(self) -> None:
        super().__init__()
        self.serve: set[str] | None = None

    def __call__(
        self, db: Session, user_id: UUID, endpoint: str, params: dict | None = None, headers: dict | None = None
    ) -> Any:
        if self.rate_limited or self.failing_all or endpoint in self.failing_endpoints:
            return super().__call__(db, user_id, endpoint, params, headers)
        if self.serve is not None and endpoint not in self.serve:
            return super().__call__(db, user_id, endpoint, params, headers)
        days = [TODAY - timedelta(days=d) for d in range(1, DAYS + 1)]
        if endpoint == SKIN_TEMPERATURE:
            self.calls.append((endpoint, params))
            return [
                {
                    "sleep_date": d.isoformat(),
                    "sleep_time_skin_temperature_celsius": 33.5,
                    "deviation_from_baseline_celsius": 0.1,
                }
                for d in days
            ]
        if endpoint in (SPO2, WRIST_ECG):
            self.calls.append((endpoint, params))
            stamp = [int(datetime(d.year, d.month, d.day, 6, tzinfo=timezone.utc).timestamp()) for d in days]
            if endpoint == SPO2:
                return [
                    {
                        "test_time": t,
                        "test_status": "SPO2_TEST_PASSED",
                        "blood_oxygen_percent": 97,
                        "heart_rate_variability_ms": 40.0,
                    }
                    for t in stamp
                ]
            return [{"test_time": t, "heart_rate_variability_ms": 41.0, "average_heart_rate_bpm": 58} for t in stamp]
        if endpoint == CONTINUOUS_HR:
            self.calls.append((endpoint, params))
            return {
                "heart_rates": [
                    {
                        "polar_user": "u",
                        "date": f"{(TODAY - timedelta(days=d)).isoformat()}T00:00:00",
                        "heart_rate_samples": [
                            {"heart_rate": 60 + i % 40, "sample_time": _hhmmss(i * 60)} for i in range(PER_DAY)
                        ],
                    }
                    for d in range(1, DAYS + 1)
                ]
            }
        if endpoint == BODY_TEMPERATURE:
            self.calls.append((endpoint, params))
            return [
                {
                    "measurement_type": "TM_SKIN_TEMPERATURE",
                    "start_time": f"{(TODAY - timedelta(days=d)).isoformat()}T00:00:00+00:00",
                    "samples": [
                        {"temperature_celsius": 33.0 + i / 1000, "recording_time_delta_milliseconds": i * 60_000}
                        for i in range(PER_DAY)
                    ],
                }
                for d in range(1, DAYS + 1)
            ]
        if endpoint == ACTIVITIES and params is not None:
            self.calls.append((endpoint, params))
            first, last = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
            return [
                {
                    "date": (first + timedelta(days=d)).isoformat(),
                    "start_time": f"{(first + timedelta(days=d)).isoformat()}T00:00:00",
                    "steps": 9000 + d,
                    "active_calories": 400 + d,
                    "distance_from_steps": 7000 + d,
                }
                for d in range((last - first).days + 1)
            ]
        return super().__call__(db, user_id, endpoint, params, headers)


class Killed(BaseException):
    """Stands in for the kernel's SIGKILL: nothing after it in the pull runs, ``except Exception`` included."""


@pytest.fixture
def polar() -> DensePolar:
    return DensePolar()


@pytest.fixture
def data_247(polar: DensePolar, monkeypatch: pytest.MonkeyPatch) -> Iterator[Polar247Data]:
    # raising=False: before the fix there is no batch size to set, and the test must fail on what it holds.
    monkeypatch.setattr(data_247_module, "_SAVE_BATCH", BATCH, raising=False)
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)
    with patch.object(Polar247Data, "_make_api_request", side_effect=polar):
        yield data


@pytest.fixture
def live_at_save() -> Iterator[list[int]]:
    """How many sample objects are alive each time a batch is handed to storage."""
    seen: list[int] = []
    real = timeseries_service.bulk_create_samples

    def spy(db: Session, samples: Any, **kwargs: Any) -> Any:
        gc.collect()
        seen.append(sum(1 for o in gc.get_objects() if type(o) is TimeSeriesSampleCreate))
        return real(db, samples, **kwargs)

    # A plain function, not a Mock: a Mock records its call arguments and would keep every batch alive.
    # Webhooks off for the same reason: conftest mocks emit_webhook_event, which keeps every payload.
    with (
        patch.object(timeseries_service, "bulk_create_samples", new=spy),
        patch("app.services.timeseries_service.svix_service.is_enabled", return_value=False),
    ):
        yield seen


def _stored(db: Session, user_id: UUID) -> int:
    return db.query(DataPointSeries).join(DataSource).filter(DataSource.user_id == user_id).count()


def _connection_id(db: Session, data_247: Polar247Data, user_id: UUID) -> UUID:
    connection = data_247.connection_repo.get_by_user_and_provider(db, user_id, "polar")
    assert connection is not None
    return connection.id


class TestAFullFetchHoldsOneBatch:
    @pytest.mark.parametrize("data_type", ["continuous_hr", "body_temperature"])
    def test_a_dense_type_never_holds_more_than_a_batch_and_a_day(
        self, db: Session, data_247: Polar247Data, live_at_save: list[int], data_type: str
    ) -> None:
        user_id = _polar_user(db)
        with patch.object(Polar247Data, "_save_missing_sleep", return_value=0):
            result = data_247.load_and_save_all(db, user_id)

        assert result[data_type] == DAYS * PER_DAY
        # The whole window is DAYS * PER_DAY = 11,200 samples. One batch, plus the day that filled it.
        assert live_at_save
        assert max(live_at_save) <= BATCH + PER_DAY

    def test_every_sample_of_a_dense_window_is_stored(
        self, db: Session, data_247: Polar247Data, polar: DensePolar, live_at_save: list[int]
    ) -> None:
        # Only the types counted below: the others' samples share timestamps and series with these.
        polar.serve = {CONTINUOUS_HR, BODY_TEMPERATURE, ACTIVITIES}
        user_id = _polar_user(db)
        with patch.object(Polar247Data, "_save_missing_sleep", return_value=0):
            result = data_247.load_and_save_all(db, user_id)

        # continuous_hr + body_temperature, 11,200 each, and 90 days of three activity series.
        activity = result["daily_activity"]
        assert activity == 91 * 3
        assert _stored(db, user_id) == 2 * DAYS * PER_DAY + activity

    def test_ninety_days_of_daily_activity_are_saved_in_batches(
        self, db: Session, data_247: Polar247Data, live_at_save: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(data_247_module, "_SAVE_BATCH", 30, raising=False)
        user_id = _polar_user(db)
        with (
            patch.object(Polar247Data, "_save_missing_sleep", return_value=0),
            patch.object(Polar247Data, "normalize_continuous_hr", return_value=[]),
            patch.object(Polar247Data, "normalize_body_temperature", return_value=[]),
        ):
            result = data_247.load_and_save_all(db, user_id)

        assert result["daily_activity"] == 91 * 3
        # 273 samples in all; a batch of 30 plus the day (three series) that filled it.
        assert max(live_at_save) <= 30 + 3


# Every type with a full form (Polar247Data._FULL_FORM_TYPES), by what its full fetch calls first.
FULL_FETCH_ENTRY = {
    "sleep": "_save_missing_sleep",
    "daily_activity": "get_daily_activity_statistics",
    "continuous_hr": "get_continuous_hr_data",
    "cardio_load": "get_cardio_load_data",
    "alertness": "get_alertness_data",
    "circadian_bedtime": "get_circadian_bedtime_data",
    "body_temperature": "get_body_temperature_data",
    "sleep_skin_temperature": "get_sleep_skin_temperature_data",
    "spo2": "get_spo2_data",
    "wrist_ecg": "get_wrist_ecg_data",
}

# Every series type a full fetch saves in batches, by endpoint and how many samples one record makes.
BATCHED = {
    "continuous_hr": (CONTINUOUS_HR, PER_DAY),
    "body_temperature": (BODY_TEMPERATURE, PER_DAY),
    "sleep_skin_temperature": (SKIN_TEMPERATURE, 2),
    "spo2": (SPO2, 2),
    "wrist_ecg": (WRIST_ECG, 2),
    "daily_activity": (ACTIVITIES, 3),
}


def test_the_tables_name_every_full_form_type() -> None:
    assert set(FULL_FETCH_ENTRY) == set(data_247_module._FULL_FORM_TYPES)


class TestASlowWebhookSend:
    """Codex on #43 (P1): with webhooks on, each commit handed its batch to a webhook thread and the
    loop built the next batch at once. A send that is slow (Redis degraded) kept every batch alive
    in its thread. The batched save sends each batch's webhooks before it builds the next."""

    def test_does_not_hold_the_saved_batches(
        self, db: Session, data_247: Polar247Data, polar: DensePolar, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        polar.serve = {CONTINUOUS_HR}
        # 28 days of 400 at 1000 a batch: nine batches of three days, then the last day alone.
        monkeypatch.setattr(data_247_module, "_SAVE_BATCH", 1000, raising=False)
        stuck: list[tuple] = []
        sent: list[int] = []
        seen: list[int] = []
        real = timeseries_service.bulk_create_samples

        class StuckThread:
            """A webhook thread that never gets to run: it holds its batch, as a blocked send would."""

            def __init__(self, target: Any, args: tuple, daemon: bool) -> None:  # noqa: ARG002
                stuck.append(args)

            def start(self) -> None:
                pass

        def emit(**kw: Any) -> None:
            sent.append(kw["sample_count"])

        def spy(db: Session, samples: Any, **kwargs: Any) -> Any:
            gc.collect()
            seen.append(sum(1 for o in gc.get_objects() if type(o) is TimeSeriesSampleCreate))
            return real(db, samples, **kwargs)

        user_id = _polar_user(db)
        with (
            patch("app.services.timeseries_service.svix_service.is_enabled", return_value=True),
            patch("app.services.timeseries_service.threading.Thread", StuckThread),
            patch("app.services.timeseries_service.on_timeseries_batch_saved", new=emit),
            patch.object(timeseries_service, "bulk_create_samples", new=spy),
            patch.object(Polar247Data, "_save_missing_sleep", return_value=0),
        ):
            result = data_247.load_and_save_all(db, user_id)

        assert result["continuous_hr"] == DAYS * PER_DAY
        assert stuck == []
        assert sum(sent) == DAYS * PER_DAY
        assert max(seen) <= 1000 + PER_DAY


class TestEverySeriesTypeIsSavedRecordByRecord:
    @pytest.mark.parametrize(("data_type", "served"), BATCHED.items())
    def test_a_batch_of_one_saves_each_record_on_its_own(
        self,
        db: Session,
        data_247: Polar247Data,
        polar: DensePolar,
        monkeypatch: pytest.MonkeyPatch,
        data_type: str,
        served: tuple[str, int],
    ) -> None:
        endpoint, per_record = served
        polar.serve = {endpoint}
        monkeypatch.setattr(data_247_module, "_SAVE_BATCH", 1, raising=False)
        sizes: list[int] = []
        real = timeseries_service.bulk_create_samples

        def spy(db: Session, samples: Any, **kwargs: Any) -> Any:
            sizes.append(len(samples))
            return real(db, samples, **kwargs)

        user_id = _polar_user(db)
        with (
            patch.object(Polar247Data, "_save_missing_sleep", return_value=0),
            patch.object(timeseries_service, "bulk_create_samples", new=spy),
        ):
            result = data_247.load_and_save_all(db, user_id)

        records = 91 if data_type == "daily_activity" else DAYS
        assert result[data_type] == records * per_record
        assert sizes == [per_record] * records


class TestAKilledFullFetch:
    @pytest.mark.parametrize(("data_type", "entry"), FULL_FETCH_ENTRY.items())
    def test_is_held_back_by_the_first_retry_gap_not_fetched_on_the_next_pull(
        self, db: Session, data_247: Polar247Data, polar: DensePolar, data_type: str, entry: str
    ) -> None:
        user_id = _polar_user(db)
        connection_id = _connection_id(db, data_247, user_id)

        def killed(*args: Any, **kwargs: Any) -> Any:
            raise Killed

        with patch.object(Polar247Data, entry, side_effect=killed), pytest.raises(Killed):
            data_247.load_and_save_all(db, user_id)
        db.rollback()
        now = datetime.now(timezone.utc)

        assert data_type not in full_due(db, connection_id, now + RETRY_FIRST_GAP - timedelta(minutes=1), [data_type])
        assert data_type in full_due(db, connection_id, now + RETRY_FIRST_GAP + timedelta(minutes=1), [data_type])

    def test_a_failure_counts_once(self, db: Session, data_247: Polar247Data, polar: DensePolar) -> None:
        polar.failing_endpoints.add(CONTINUOUS_HR)
        user_id = _polar_user(db)
        connection_id = _connection_id(db, data_247, user_id)

        with patch.object(Polar247Data, "_save_missing_sleep", return_value=0):
            data_247.load_and_save_all(db, user_id)

        state = db.get(PolarPullState, connection_id)
        assert state is not None
        db.refresh(state)
        assert state.full_retry["continuous_hr"]["failures"] == 1

    def test_a_full_fetch_that_succeeds_leaves_no_retry_gap(
        self, db: Session, data_247: Polar247Data, polar: DensePolar
    ) -> None:
        user_id = _polar_user(db)
        connection_id = _connection_id(db, data_247, user_id)

        with patch.object(Polar247Data, "_save_missing_sleep", return_value=0):
            data_247.load_and_save_all(db, user_id)

        state = db.get(PolarPullState, connection_id)
        assert state is not None
        db.refresh(state)
        assert "continuous_hr" not in (state.full_retry or {})
        assert "continuous_hr" in state.full_fetched_at


INTERRUPTED = "polar_full_fetch_interrupted"


def _killed(*args: Any, **kwargs: Any) -> Any:
    raise Killed


def _interrupted_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    lines = []
    for line in capsys.readouterr().out.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("action") == INTERRUPTED:
            lines.append(entry)
    return lines


def _kill_a_pull(db: Session, data_247: Polar247Data, user_id: UUID, entry: str) -> None:
    with patch.object(Polar247Data, entry, side_effect=_killed), pytest.raises(Killed):
        data_247.load_and_save_all(db, user_id)
    db.rollback()


class TestAnInterruptedFullFetch:
    """The pull after a killed full fetch says so: the type reads FAILED with a code, once, and
    a log line names it. A fetch that failed and was caught is not an interruption."""

    @pytest.mark.parametrize(("data_type", "entry"), FULL_FETCH_ENTRY.items())
    def test_the_next_pull_reports_it_failed_once(
        self,
        db: Session,
        data_247: Polar247Data,
        polar: DensePolar,
        capsys: pytest.CaptureFixture[str],
        data_type: str,
        entry: str,
    ) -> None:
        user_id = _polar_user(db)
        _kill_a_pull(db, data_247, user_id, entry)
        capsys.readouterr()

        result = data_247.load_and_save_all(db, user_id)

        outcome = result[data_type]
        assert isinstance(outcome, DataTypeFailure)
        assert outcome.code == INTERRUPTED
        assert [line["data_type"] for line in _interrupted_lines(capsys)] == [data_type]

        again = data_247.load_and_save_all(db, user_id)

        assert not isinstance(again.get(data_type), DataTypeFailure)
        assert _interrupted_lines(capsys) == []

    def test_a_failure_that_was_caught_is_not_an_interruption(
        self, db: Session, data_247: Polar247Data, polar: DensePolar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        polar.failing_endpoints.add(CONTINUOUS_HR)
        user_id = _polar_user(db)
        data_247.load_and_save_all(db, user_id)
        capsys.readouterr()

        result = data_247.load_and_save_all(db, user_id)

        assert "continuous_hr" not in result
        assert _interrupted_lines(capsys) == []

    def test_once_its_gap_has_passed_it_runs_and_reports_its_own_outcome(
        self, db: Session, data_247: Polar247Data, polar: DensePolar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user_id = _polar_user(db)
        _kill_a_pull(db, data_247, user_id, "get_continuous_hr_data")
        capsys.readouterr()

        with freeze_time(datetime.now(timezone.utc) + RETRY_FIRST_GAP + timedelta(minutes=2)):
            result = data_247.load_and_save_all(db, user_id)

        assert result["continuous_hr"] == DAYS * PER_DAY
        assert [line["data_type"] for line in _interrupted_lines(capsys)] == ["continuous_hr"]
