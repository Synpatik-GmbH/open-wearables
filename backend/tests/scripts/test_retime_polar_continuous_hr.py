"""2.96.8 (spec v1.1 AC9): one dev account's Polar heart rate is stored again at its true instants.

Rows are seeded the way the pre-2.96.8 image stored them: continuous heart rate one offset late
(local wall time read as UTC), the sleep record's own samples at their true instants, in the
same data source. See scripts/data_migrations/retime_polar_continuous_hr.py.
"""

import importlib.util
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, EventRecord, PolarPullState, UserConnection
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.enums.series_types import get_series_type_id
from app.services.providers.polar.data_247 import Polar247Data, _ContinuousHrOffsets
from app.services.providers.polar.strategy import PolarStrategy
from app.services.timeseries_service import timeseries_service
from tests.factories import UserConnectionFactory, UserFactory

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "retime_polar_continuous_hr.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("retime_polar_continuous_hr", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_module()
retime = script.retime_polar_continuous_hr

TODAY = datetime.now(timezone.utc).date()
NIGHT = TODAY - timedelta(days=3)  # the night that ended on NIGHT, 23:00 to 07:00 at +2
EVE = NIGHT - timedelta(days=1)
EDGE = TODAY - timedelta(days=27)  # the first day of the window; a night Polar lists, not stored here, ends after it
FAR = TODAY - timedelta(days=20)  # no Polar night or workout within 3 days: P3, uncorrected
OLD = TODAY - timedelta(days=40)  # before the window: never touched
HOURS = [f"{h:02d}:00" for h in range(24)]


def _sleep() -> dict[str, Any]:
    return {
        "date": NIGHT.isoformat(),
        "sleep_start_time": f"{EVE}T23:00:00+02:00",
        "sleep_end_time": f"{NIGHT}T07:00:00+02:00",
        "light_sleep": 14400,
        "deep_sleep": 5400,
        "rem_sleep": 7200,
        "heart_rate_samples": {"23:30": 55, "02:30": 47, "06:30": 52},
    }


def _listing() -> dict[str, Any]:
    after_edge = EDGE + timedelta(days=1)
    return {
        "available": [
            {"date": NIGHT.isoformat(), "start_time": f"{EVE}T23:00:00+02:00", "end_time": f"{NIGHT}T07:00:00+02:00"},
            {
                "date": after_edge.isoformat(),
                "start_time": f"{EDGE}T23:00:00+02:00",
                "end_time": f"{after_edge}T07:00:00+02:00",
            },
        ]
    }


def _day(on: date, bpm: int) -> dict[str, Any]:
    return {
        "polar_user": "https://www.polaraccesslink.com/v3/users/123",
        "date": on.isoformat(),
        "heart_rate_samples": [{"heart_rate": bpm, "sample_time": t} for t in HOURS],
    }


class FakePolar:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.listing = _listing()
        self.days = [_day(EDGE, 75), _day(EVE, 70), _day(NIGHT, 60), _day(FAR, 65)]
        self.failing: set[str] = set()

    def __call__(
        self, db: Session, user_id: UUID, endpoint: str, params: dict | None = None, headers: dict | None = None
    ) -> Any:  # noqa: ARG002
        self.calls.append(endpoint)
        if any(endpoint.startswith(f) for f in self.failing):
            raise HTTPException(status_code=503, detail="down")
        if endpoint == "/v3/users/sleep/available":
            return self.listing
        if endpoint == "/v3/users/continuous-heart-rate":
            return {"heart_rates": [dict(d) for d in self.days]}
        if endpoint == f"/v3/users/sleep/{NIGHT}":
            return _sleep()
        return {}


@pytest.fixture
def polar() -> FakePolar:
    return FakePolar()


@pytest.fixture
def data(polar: FakePolar) -> Any:
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)
    with patch.object(Polar247Data, "_make_api_request", side_effect=polar):
        yield data


def _seed(db: Session, data: Polar247Data) -> UUID:
    """A Polar member as the pre-2.96.8 image left them."""
    user = UserFactory()
    UserConnectionFactory(user=user, provider="polar", status=ConnectionStatus.ACTIVE)
    db.commit()
    data._save_sleep_items(db, user.id, [_sleep()])
    late = data.normalize_continuous_hr(
        [_day(EDGE, 75), _day(EVE, 70), _day(NIGHT, 60), _day(FAR, 65), _day(OLD, 80)],
        user.id,
        _ContinuousHrOffsets({}),
    )
    timeseries_service.crud.bulk_create(db, late)
    db.commit()
    return user.id


def _rows(db: Session, user_id: UUID) -> list[tuple[datetime, int]]:
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


def _at(on: date, hhmm: str, minus_hours: int = 0) -> datetime:
    return datetime.combine(on, time.fromisoformat(hhmm), tzinfo=timezone.utc) - timedelta(hours=minus_hours)


def _expected() -> list[tuple[datetime, int]]:
    # Continuous at true instants (EVE and NIGHT at +2 from the night; FAR uncorrected), the
    # night's own samples, and the row before the window untouched.
    rows = {_at(OLD, t): 80 for t in HOURS}
    rows.update({_at(EDGE, t, 2): 75 for t in HOURS})
    rows.update({_at(EVE, t, 2): 70 for t in HOURS})
    rows.update({_at(NIGHT, t, 2): 60 for t in HOURS})
    rows.update({_at(FAR, t): 65 for t in HOURS})
    rows.update({_at(EVE, "23:30", 2): 55, _at(NIGHT, "02:30", 2): 47, _at(NIGHT, "06:30", 2): 52})
    return sorted(rows.items())


def _sleep_records(db: Session, user_id: UUID) -> list[tuple[UUID, datetime, datetime]]:
    rows = (
        db.query(EventRecord.id, EventRecord.start_datetime, EventRecord.end_datetime)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_id == user_id, EventRecord.category == "sleep")
        .all()
    )
    return [(r.id, r.start_datetime, r.end_datetime) for r in rows]


def _marked(db: Session, user_id: UUID) -> bool:
    connection = db.query(UserConnection).filter(UserConnection.user_id == user_id).one()
    state = db.get(PolarPullState, connection.id)
    db.refresh(state) if state is not None else None
    return state is not None and script.MARKER in (state.full_fetched_at or {})


class TestRetime:
    def test_the_account_is_stored_again_at_true_instants_with_no_duplicate(
        self, db: Session, data: Polar247Data, polar: FakePolar, capsys: Any
    ) -> None:
        user_id = _seed(db, data)
        nights = _sleep_records(db, user_id)
        assert _rows(db, user_id) != _expected()

        report = retime(db, user_id, data)

        assert _rows(db, user_id) == _expected()
        assert report.outcome == "done"
        assert (report.continuous, report.sleep, report.uncorrected) == (96, 3, 24)
        assert _sleep_records(db, user_id) == nights, "sleep records are not touched"
        assert _marked(db, user_id)
        assert "uncorrected=24" in capsys.readouterr().out

    def test_another_members_rows_are_not_touched(self, db: Session, data: Polar247Data) -> None:
        other = _seed(db, data)
        before = _rows(db, other)
        user_id = _seed(db, data)

        retime(db, user_id, data)

        assert _rows(db, other) == before

    def test_it_runs_once(self, db: Session, data: Polar247Data, polar: FakePolar) -> None:
        user_id = _seed(db, data)
        retime(db, user_id, data)
        after = _rows(db, user_id)
        polar.calls.clear()

        report = retime(db, user_id, data)

        assert report.outcome == "already_done"
        assert polar.calls == []
        assert _rows(db, user_id) == after

    def test_a_stored_night_polar_no_longer_lists_stops_it_before_any_change(
        self, db: Session, data: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _seed(db, data)
        before = _rows(db, user_id)
        polar.listing = {"available": []}

        report = retime(db, user_id, data)

        assert report.stopped
        assert report.unlisted_nights == 1
        assert _rows(db, user_id) == before
        assert not _marked(db, user_id)
        assert "/v3/users/continuous-heart-rate" not in polar.calls

    @pytest.mark.parametrize(
        "endpoint", ["/v3/users/sleep/available", "/v3/users/continuous-heart-rate", f"/v3/users/sleep/{NIGHT}"]
    )
    def test_a_polar_call_that_fails_changes_nothing(
        self, db: Session, data: Polar247Data, polar: FakePolar, endpoint: str
    ) -> None:
        user_id = _seed(db, data)
        before = _rows(db, user_id)
        polar.failing = {endpoint}

        with pytest.raises(HTTPException):
            retime(db, user_id, data)

        assert _rows(db, user_id) == before
        assert not _marked(db, user_id)


class TestGate:
    def test_without_the_variable_it_does_nothing(self, db: Session, data: Polar247Data, polar: FakePolar) -> None:
        user_id = _seed(db, data)
        before = _rows(db, user_id)

        assert retime(db, None, data).outcome == "not_configured"
        assert polar.calls == []
        assert _rows(db, user_id) == before

    def test_an_id_with_no_active_polar_connection_does_nothing(
        self, db: Session, data: Polar247Data, polar: FakePolar
    ) -> None:
        # Production: the dev account's id names no user there.
        assert retime(db, uuid4(), data).outcome == "no_active_polar_connection"
        assert polar.calls == []

    def test_a_revoked_connection_does_nothing(self, db: Session, data: Polar247Data, polar: FakePolar) -> None:
        user_id = _seed(db, data)
        db.query(UserConnection).filter(UserConnection.user_id == user_id).update({"status": ConnectionStatus.REVOKED})
        db.commit()

        assert retime(db, user_id, data).outcome == "no_active_polar_connection"
        assert polar.calls == []

    def test_a_replica_already_running_it_is_left_to_it(
        self, db: Session, data: Polar247Data, polar: FakePolar
    ) -> None:
        user_id = _seed(db, data)
        with db.get_bind().engine.connect() as other:
            assert other.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": script._LOCK_KEY}).scalar()
            try:
                assert retime(db, user_id, data).outcome == "running_elsewhere"
            finally:
                other.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": script._LOCK_KEY})
                other.commit()
        assert polar.calls == []

    @pytest.mark.parametrize(("value", "expected"), [("", None), ("  ", None)])
    def test_an_empty_variable_is_unset(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: None) -> None:
        monkeypatch.setenv(script.USER_ID_VARIABLE, value)
        assert script._configured_user_id() is expected

    def test_the_variable_names_the_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid4()
        monkeypatch.setenv(script.USER_ID_VARIABLE, str(user_id))
        assert script._configured_user_id() == user_id
