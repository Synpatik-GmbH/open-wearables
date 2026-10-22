"""Night evidence counts only for a sleep that was actually stored (Codex P1 on #46 at 0100f1f1)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import DataPointSeries, EventRecord, User, UserConnection
from app.models.wearable_history_source import WearableHistorySource
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository
from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository as Gate
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import SeriesType
from app.schemas.model_crud.activities import EventRecordCreate, TimeSeriesSampleCreate
from app.schemas.sync_status import DataTypeFailure
from app.services.providers.oura.data_247 import Oura247Data
from app.utils.wearable_history_ingress import (
    PreStartDataRejectedError,
    WearableHistoryMetadataUnavailableError,
)
from tests.factories import DataSourceFactory, UserConnectionFactory, UserFactory


def provisional_source(db: Session) -> tuple:
    member = UserFactory()
    # Connected in the future, so the source stays provisional whatever the test clock reads.
    connected = datetime.now(UTC) + timedelta(days=2)
    source = WearableHistoryRepository().begin_source(db, member.id, "garmin", connected)
    data_source = DataSourceFactory(user=member, provider="garmin", source="garmin")
    db.commit()
    return member, data_source, source.connection_day


def sleep_creator(member: User, night_start: datetime) -> EventRecordCreate:
    return EventRecordCreate(
        id=uuid4(),
        user_id=member.id,
        source="garmin",
        source_name="Garmin",
        category="sleep",
        type="sleep_session",
        start_datetime=night_start,
        end_datetime=night_start + timedelta(hours=8),
        zone_offset="+00:00",
    )


def test_failed_single_sleep_write_leaves_no_night_evidence(db: Session) -> None:
    member, data_source, connection_day = provisional_source(db)
    night_start = datetime.combine(connection_day - timedelta(days=11), datetime.min.time(), UTC) + timedelta(hours=22)
    record = EventRecord(
        id=uuid4(),
        data_source_id=data_source.id,
        category="sleep",
        type="sleep_session",
        source_name="Garmin",
        start_datetime=night_start,
        end_datetime=night_start + timedelta(hours=8),
        zone_offset="+00:00",
    )

    def failing_write() -> None:
        raise RuntimeError("detail write failed")

    with pytest.raises(RuntimeError, match="detail write failed"):
        WearableHistoryIngressRepository(db).persist_event(record, failing_write)
    db.commit()  # A provider that catches the failure and carries on still commits.
    db.expire_all()
    source = WearableHistoryRepository().get(db, member.id, "garmin")
    assert source.night_dates == []
    assert source.start_date == connection_day


@pytest.mark.parametrize("wrapped", [False, True])
def test_sleep_write_rejected_by_a_constraint_leaves_no_night_evidence(db: Session, wrapped: bool) -> None:
    """The rejection branch decides admission after the rollback; it must not record the night again."""

    member, data_source, connection_day = provisional_source(db)
    night_start = datetime.combine(connection_day - timedelta(days=11), datetime.min.time(), UTC) + timedelta(hours=22)
    record = EventRecord(
        id=uuid4(),
        data_source_id=data_source.id,
        category="sleep",
        type="sleep_session",
        source_name="Garmin",
        start_datetime=night_start,
        end_datetime=night_start + timedelta(hours=8),
        zone_offset="+00:00",
    )
    violation = IntegrityError("INSERT INTO sleep_details", {}, Exception("null value in column"))

    def rejected_write() -> None:
        if wrapped:
            # Decorated repositories surface SQL errors as HTTPException over the IntegrityError.
            raise HTTPException(status_code=500) from violation
        raise violation

    with pytest.raises((IntegrityError, HTTPException)):
        WearableHistoryIngressRepository(db).persist_event(record, rejected_write)
    db.commit()  # A provider that catches the failure and carries on still commits.
    db.expire_all()
    source = WearableHistoryRepository().get(db, member.id, "garmin")
    assert source.night_dates == []
    assert source.start_date == connection_day


def test_failed_bulk_sleep_insert_leaves_no_night_evidence(db: Session) -> None:
    member, _, connection_day = provisional_source(db)
    night_start = datetime.combine(connection_day - timedelta(days=11), datetime.min.time(), UTC) + timedelta(hours=22)
    repository = EventRecordRepository(EventRecord)
    with (
        patch.object(
            repository.data_source_repo, "batch_ensure_data_sources", side_effect=RuntimeError("insert failed")
        ),
        pytest.raises(Exception, match="insert failed"),
    ):
        repository.bulk_create(db, [sleep_creator(member, night_start)])
    db.commit()
    db.expire_all()
    source = WearableHistoryRepository().get(db, member.id, "garmin")
    assert source.night_dates == []
    assert source.start_date == connection_day


def test_connection_without_history_row_begins_its_source_from_the_connection_clock(db: Session) -> None:
    """Codex P2 on #46: a failed history write after the OAuth commit must not strand the connection."""

    member = UserFactory()
    WearableHistoryRepository().begin_source(db, member.id, "apple")  # Another source has authority.
    UserConnectionFactory(user=member, provider="polar")
    # The factory begins the source as OAuth does; remove it to model a failed history write.
    db.query(WearableHistorySource).filter(
        WearableHistorySource.user_id == member.id, WearableHistorySource.provider == "polar"
    ).delete()
    connected_at = datetime(2026, 10, 18, 21, 30, tzinfo=UTC)  # 23:30 Berlin, still the 18th
    db.query(UserConnection).filter(UserConnection.user_id == member.id).update(
        {UserConnection.created_at: connected_at}
    )
    db.commit()
    sample = TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=member.id,
        provider="polar",
        source="polar",
        recorded_at=datetime.now(UTC),
        series_type=SeriesType.heart_rate,
        value=60,
    )
    assert DataPointSeriesRepository(DataPointSeries).bulk_create(db, [sample]).inserted == 1
    source = WearableHistoryRepository().get(db, member.id, "polar")
    assert source is not None
    assert source.connection_day.isoformat() == "2026-10-18"


def test_xml_importer_alias_is_judged_by_the_apple_source() -> None:
    """Codex P1 on #46: XML sleep names its provider "apple_health_xml"."""

    member = uuid4()
    gate = Gate.__new__(Gate)
    sleep = SimpleNamespace(category="sleep", user_id=member, provider="apple_health_xml")
    assert gate.identity(sleep) == (member, "apple")


def test_xml_sleep_with_an_apple_source_is_admitted_instead_of_raising(db: Session) -> None:
    member = UserFactory()
    connected = datetime.now(UTC) + timedelta(days=2)
    WearableHistoryRepository().begin_source(db, member.id, "apple", connected)
    db.commit()
    night_start = datetime.now(UTC) - timedelta(hours=10)
    sleep = SimpleNamespace(
        category="sleep",
        user_id=member.id,
        provider="apple_health_xml",
        start_datetime=night_start,
        end_datetime=night_start + timedelta(hours=8),
        zone_offset="+00:00",
    )
    assert WearableHistoryIngressRepository(db).event_admitted(sleep) is True


@pytest.mark.parametrize("failure", ["metadata", "sql_boundary"])
def test_oura_retriable_history_error_holds_the_pull_cursor(failure: str) -> None:
    """Codex P1 on #46: a swallowed retry error let the cursor pass an acknowledged sample.

    sql_boundary: a save's own commit meets the deferred wh_ingress check after the freeze
    instant; the whole type rolled back, so the cursor must not pass it either.
    """

    error: Exception = (
        WearableHistoryMetadataUnavailableError(failure)
        if failure == "metadata"
        else IntegrityError("COMMIT", {}, _BoundaryRejectionError("commit rejected"))
    )
    service = Oura247Data("oura", "https://provider.test", MagicMock())
    db = MagicMock()
    with patch.multiple(
        service,
        **{
            name: MagicMock(return_value=[])
            for name in dir(service)
            if name.startswith("get_") and name.endswith("_data") or name == "get_personal_info"
        },
    ):
        saves = {name: MagicMock(return_value=0) for name in dir(service) if name.startswith("save_")}
        saves["save_heart_rate_data"] = MagicMock(side_effect=error)
        with patch.multiple(service, **saves):
            results = service.load_and_save_all(db, uuid4())
    assert isinstance(results["heart_rate"], DataTypeFailure)
    if failure == "metadata":
        assert results["heart_rate"].code == type(error).__name__
    assert results["sleep"] == 0  # Ordinary types still report their counts.


class _BoundaryDiag:
    message_primary = "Frozen source rejects pre-start data"


class _BoundaryRejectionError(Exception):
    """A deferred wh_ingress rejection as psycopg raises it at commit."""

    diag = _BoundaryDiag()


@pytest.mark.parametrize("failure", ["metadata", "sql_boundary"])
def test_oura_sleep_history_error_inside_save_reaches_the_cursor_classifier(failure: str) -> None:
    """Codex P1 on #46 at 8546b1ae: save_sleep_data's own catch swallowed the retriable error."""

    error = {
        "metadata": WearableHistoryMetadataUnavailableError("metadata"),
        "sql_boundary": _BoundaryRejectionError("commit rejected"),
    }[failure]
    service = Oura247Data("oura", "https://provider.test", MagicMock())
    night = datetime(2026, 10, 12, 22, tzinfo=UTC)
    sleep = {
        "id": str(uuid4()),
        "start_time": night.isoformat(),
        "end_time": (night + timedelta(hours=8)).isoformat(),
        "timezone_offset": "+00:00",
        "stages": {},
    }
    getters = {
        name: MagicMock(return_value=[])
        for name in dir(service)
        if (name.startswith("get_") and name.endswith("_data")) or name == "get_personal_info"
    }
    getters["get_sleep_data"] = MagicMock(return_value=[sleep])
    others = {
        name: MagicMock(return_value=0)
        for name in dir(service)
        if name.startswith("save_") and name != "save_sleep_data"
    }
    with (
        patch.multiple(service, **getters, **others),
        patch.object(service, "normalize_sleeps", return_value=[sleep]),
        patch(
            "app.services.providers.oura.data_247.event_record_service.create_or_merge_sleep",
            side_effect=error,
        ),
    ):
        results = service.load_and_save_all(MagicMock(), uuid4())
    assert isinstance(results["sleep"], DataTypeFailure)
    expected = WearableHistoryMetadataUnavailableError if failure == "sql_boundary" else type(error)
    assert results["sleep"].code == expected.__name__


def frozen_garmin_record(db: Session) -> EventRecord:
    """A sleep from before a frozen source's start date: the gate rejects it."""

    member = UserFactory()
    connected = datetime.now(UTC) - timedelta(days=20)
    source = WearableHistoryRepository().begin_source(db, member.id, "garmin", connected)
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    data_source = DataSourceFactory(user=member, provider="garmin", source="garmin")
    db.commit()
    night_start = datetime.combine(source.start_date - timedelta(days=3), datetime.min.time(), UTC) + timedelta(
        hours=22
    )
    return EventRecord(
        id=uuid4(),
        data_source_id=data_source.id,
        category="sleep",
        type="sleep_session",
        source_name="Garmin",
        start_datetime=night_start,
        end_datetime=night_start + timedelta(hours=8),
        zone_offset="+00:00",
    )


@pytest.mark.parametrize("outcome", ["admitted", "rejected_after_write", "failed"])
def test_a_rolled_back_item_leaves_no_after_commit_callback(db: Session, outcome: str) -> None:
    """Codex P1 on #46 at 37cb004c: webhooks registered by a rolled-back item fired on a later commit.

    A savepoint rollback does not remove an after_commit listener, so the gate drops the
    listeners an item registered whenever that item's savepoint rolls back.
    """

    if outcome in ("admitted", "failed"):
        member, data_source, connection_day = provisional_source(db)
        night_start = datetime.combine(connection_day - timedelta(days=2), datetime.min.time(), UTC) + timedelta(
            hours=22
        )
        record = EventRecord(
            id=uuid4(),
            data_source_id=data_source.id,
            category="sleep",
            type="sleep_session",
            source_name="Garmin",
            start_datetime=night_start,
            end_datetime=night_start + timedelta(hours=8),
            zone_offset="+00:00",
        )
    else:
        record = frozen_garmin_record(db)
    fired: list[str] = []

    def write() -> None:
        sa_event.listen(db, "after_commit", lambda _session: fired.append("webhook"), once=True)
        if outcome == "rejected_after_write":
            # The freeze crossed after the gate admitted the item: the deferred check rejects it.
            raise PreStartDataRejectedError("Frozen source rejects pre-start data")
        if outcome == "failed":
            raise IntegrityError("INSERT INTO sleep_details", {}, Exception("null value in column"))

    gate = WearableHistoryIngressRepository(db)
    if outcome == "rejected_after_write":
        with patch.object(gate, "filter_events", side_effect=lambda records: records):
            assert gate.persist_event(record, write) is None
    elif outcome == "failed":
        with patch.object(gate, "filter_events", side_effect=lambda records: records), pytest.raises(IntegrityError):
            gate.persist_event(record, write)
    else:
        gate.persist_event(record, write)
    db.commit()  # A provider that carries on after the item still commits.
    assert fired == (["webhook"] if outcome == "admitted" else [])
