"""Persisted regressions for the current-head archive, item and SDK review classes."""

import json
from datetime import UTC, date, datetime, timedelta
from logging import getLogger
from time import sleep
from types import SimpleNamespace
from typing import Any, Never
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.routes.v1.sdk_sync import sync_sdk_data
from app.config import settings
from app.constants.series_types.sdk import SleepPhase
from app.models import DataPointSeries, DataPointSeriesArchive, DataSource, EventRecord
from app.repositories.archival_repository import DataPointSeriesArchiveRepository
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.model_crud.activities import EventRecordCreate, EventRecordDetailCreate
from app.services.event_record_service import EventRecordService
from app.services.sdk.import_service import import_service
from app.services.sdk.sleep_service import _apply_transition, _create_new_sleep_state
from tests.factories import DataPointSeriesFactory, DataSourceFactory, EventRecordFactory, UserFactory
from tests.services.test_wearable_history_ingress_closure import frozen_source
from tests.services.test_wearable_history_review_closure import envelope


@pytest.mark.parametrize("transition", ["freeze", "first-night"])
def test_archive_deletes_only_the_buckets_actually_upserted(db: Session, transition: str) -> None:
    member = UserFactory()
    history = WearableHistoryRepository().begin_source(db, member.id, "apple")
    own = DataSourceFactory(user=member, provider="apple")
    other = DataSourceFactory(user=member, provider="polar")
    WearableHistoryRepository().begin_source(db, member.id, "polar")
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    hr_time = day + timedelta(hours=12) if transition == "freeze" else day - timedelta(hours=4)
    heart = DataPointSeriesFactory(
        data_source=own,
        recorded_at=hr_time,
        series_type_definition_id=get_series_type_id(SeriesType.heart_rate),
        value=61,
    )
    other_heart = DataPointSeriesFactory(
        data_source=other,
        recorded_at=hr_time,
        series_type_definition_id=get_series_type_id(SeriesType.heart_rate),
        value=62,
    )
    for source, value in [(own, 72), (other, 73)]:
        db.add(
            DataPointSeries(
                id=uuid4(),
                data_source_id=source.id,
                recorded_at=hr_time,
                series_type_definition_id=get_series_type_id(SeriesType.weight),
                value=value,
            )
        )
    history.freeze_at = (
        datetime.now(UTC) + timedelta(seconds=1) if transition == "freeze" else datetime.now(UTC) - timedelta(seconds=1)
    )
    db.commit()
    deadline = history.freeze_at
    changed = False

    def after_aggregate(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        nonlocal changed
        if not changed and statement.lstrip().startswith("INSERT INTO data_point_series_archive"):
            changed = True
            if transition == "freeze":
                while datetime.now(UTC) <= deadline:
                    sleep(0.01)
            else:
                db.connection().execute(
                    insert(EventRecord).values(
                        id=uuid4(),
                        data_source_id=own.id,
                        category="sleep",
                        type="sleep_session",
                        source_name="watch",
                        start_datetime=day - timedelta(hours=4),
                        end_datetime=day + timedelta(hours=4),
                        zone_offset="+00:00",
                    )
                )

    event.listen(db.get_bind(), "before_cursor_execute", after_aggregate)
    try:
        with patch("app.repositories.archival_repository.MAX_ROWS_PER_RUN", 1):
            assert DataPointSeriesArchiveRepository().archive_data_before(db, (day + timedelta(days=1)).date()) == 2
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", after_aggregate)
    assert changed
    assert db.get(DataPointSeries, heart.id) is not None
    assert db.get(DataPointSeries, other_heart.id) is not None
    archives = list(
        db.scalars(select(DataPointSeriesArchive).where(DataPointSeriesArchive.data_source_id.in_([own.id, other.id])))
    )
    assert {(a.series_type_definition_id, float(a.value)) for a in archives} == {
        (get_series_type_id(SeriesType.weight), 72),
        (get_series_type_id(SeriesType.weight), 73),
    }


@pytest.mark.parametrize("path", ["orm", "core", "wrapped"])
@pytest.mark.parametrize("prior", ["orm", "core"])
def test_final_item_rejection_preserves_prior_pending_writes(db: Session, path: str, prior: str) -> None:
    member = UserFactory()
    history = WearableHistoryRepository().begin_source(db, member.id, "apple")
    own = DataSourceFactory(user=member, provider="apple")
    history.freeze_at = datetime.now(UTC) + timedelta(seconds=1)
    db.commit()
    deadline = history.freeze_at
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    sample_id = uuid4()
    values = dict(
        id=sample_id,
        data_source_id=own.id,
        series_type_definition_id=get_series_type_id(SeriesType.heart_rate),
        recorded_at=day + timedelta(hours=12),
        value=61,
    )
    if prior == "orm":
        db.add(DataPointSeries(**values))
    else:
        db.execute(insert(DataPointSeries).values(**values))
    record = EventRecordCreate(
        id=uuid4(),
        user_id=member.id,
        source="apple",
        category="workout",
        type="running",
        source_name="watch",
        start_datetime=day - timedelta(hours=12),
        end_datetime=day - timedelta(hours=11),
    )
    repo = EventRecordRepository(EventRecord)

    def write() -> EventRecord | None:
        while datetime.now(UTC) <= deadline:
            sleep(0.01)
        if path == "orm":
            return repo.create_and_flush(db, record)
        try:
            db.execute(
                insert(EventRecord).values(
                    id=record.id,
                    data_source_id=own.id,
                    category="workout",
                    source_name="watch",
                    start_datetime=record.start_datetime,
                    end_datetime=record.end_datetime,
                )
            )
            return db.get(EventRecord, record.id)
        except IntegrityError as exc:
            if path == "wrapped":
                raise HTTPException(status_code=400, detail="wrapped repository failure") from exc
            raise

    assert WearableHistoryIngressRepository(db).persist_event(record, write) is None
    admitted = record.model_copy(
        update={"id": uuid4(), "start_datetime": day + timedelta(hours=12), "end_datetime": day + timedelta(hours=13)}
    )
    assert EventRecordService(getLogger(__name__)).create_if_admitted(db, admitted) is not None
    db.commit()
    assert db.get(DataPointSeries, sample_id) is not None
    assert db.get(EventRecord, record.id) is None
    assert db.get(EventRecord, admitted.id) is not None


@pytest.mark.parametrize(
    "provider", ["garmin", "suunto", "whoop", "oura", "polar", "ultrahuman", "google_health", "withings"]
)
def test_sleep_merge_skips_held_item_and_retains_two_admitted_neighbours(db: Session, provider: str) -> None:
    member, history = frozen_source(db, provider)
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    service = EventRecordService(getLogger(__name__))
    for index, t in enumerate(
        [day - timedelta(days=2, hours=4), day - timedelta(hours=4), day + timedelta(days=1, hours=-4)]
    ):
        record = EventRecordCreate(
            id=uuid4(),
            user_id=member.id,
            source=provider,
            provider=provider,
            category="sleep",
            type="sleep_session",
            source_name="watch",
            start_datetime=t,
            end_datetime=t + timedelta(hours=8),
            zone_offset="+00:00",
        )
        detail = EventRecordDetailCreate(
            record_id=record.id, sleep_total_duration_minutes=480, sleep_time_in_bed_minutes=480
        )
        result = service.create_or_merge_sleep(db, member.id, record, detail, threshold_minutes=60)
        assert (result is not None) == (index > 0)
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert (
        db.scalar(
            text("SELECT count(*) FROM event_record e JOIN data_source d ON d.id=e.data_source_id WHERE d.user_id=:u"),
            {"u": member.id},
        )
        == 2
    )


def test_frozen_sleep_merge_does_not_absorb_a_held_adjacent_nap(db: Session) -> None:
    member = UserFactory()
    history = WearableHistoryRepository().begin_source(db, member.id, "apple")
    own = DataSourceFactory(user=member, provider="apple", source="apple")
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    nap = EventRecordFactory(
        data_source=own,
        category="sleep",
        type="sleep_session",
        source_name="watch",
        start_datetime=day - timedelta(hours=7),
        end_datetime=day - timedelta(hours=4),
        zone_offset="+00:00",
    )
    history.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    record = EventRecordCreate(
        id=uuid4(),
        user_id=member.id,
        source="apple",
        provider="apple",
        category="sleep",
        type="sleep_session",
        source_name="watch",
        start_datetime=day - timedelta(hours=4),
        end_datetime=day + timedelta(hours=4),
        zone_offset="+00:00",
    )
    result = EventRecordService(getLogger(__name__)).create_or_merge_sleep(
        db, member.id, record, EventRecordDetailCreate(record_id=record.id, sleep_total_duration_minutes=480), 60
    )
    assert result is not None
    assert result.start_datetime == record.start_datetime
    assert db.get(EventRecord, nap.id) is not None


@pytest.mark.parametrize("wrapper", ["in_bed", "sleeping"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("offload", [False, True])
def test_sdk_first_night_uses_the_importers_cleaned_window(
    db: Session, wrapper: str, existing: bool, offload: bool
) -> None:
    member, history = frozen_source(db)
    history.start_date = date(2026, 10, 13)
    db.flush()
    raw_start = datetime(2026, 10, 12, 15, tzinfo=UTC)
    clean_start = datetime(2026, 10, 12, 20, tzinfo=UTC)
    end = datetime(2026, 10, 13, 6, tzinfo=UTC)
    state = None
    if existing:
        state = _create_new_sleep_state(raw_start, end, provider="apple")
        phase = SleepPhase.IN_BED if wrapper == "in_bed" else SleepPhase.SLEEPING
        state = _apply_transition(db, str(member.id), state, phase, raw_start, end, "apple")
    raw = envelope(
        {
            "sleep": (
                [] if existing else [{"stage": wrapper, "startDate": raw_start.isoformat(), "endDate": end.isoformat()}]
            )
            + [{"stage": "light", "startDate": clean_start.isoformat(), "endDate": end.isoformat()}],
            "records": [
                {
                    "type": "HEART_RATE",
                    "value": 61 + i,
                    "unit": "count",
                    "startDate": t.isoformat(),
                    "endDate": t.isoformat(),
                }
                for i, t in enumerate([clean_start, end])
            ],
        }
    )
    with (
        patch.object(settings, "sdk_payload_s3_offload", offload),
        patch("app.api.routes.v1.sdk_sync.load_sleep_state", return_value=state),
        patch("app.api.routes.v1.sdk_sync.store_raw_payload"),
        patch("app.api.routes.v1.sdk_sync.put_payload_to_s3", return_value="s3://test-bucket/clean") as s3,
        patch("app.api.routes.v1.sdk_sync.process_sdk_upload.delay") as queue,
    ):
        assert sync_sdk_data(str(member.id), raw, SimpleNamespace(auth_type="api_key"), db).status_code == 202
    stored = json.loads(s3.call_args.kwargs["payload"] if offload else queue.call_args.kwargs["content"])
    assert len(stored["data"]["sleep"]) == (1 if existing else 2)
    assert len(stored["data"]["records"]) == 2
    with (
        # import_service reads the state for the history gate; sleep_service reads it to merge.
        patch("app.services.sdk.import_service.load_sleep_state", return_value=state),
        patch("app.services.sdk.sleep_service.load_sleep_state", return_value=state),
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps.delay"),
    ):
        result = import_service.load_data(db, stored, str(member.id))
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    sleeps = list(db.scalars(select(EventRecord).join(DataSource).where(DataSource.user_id == member.id)))
    assert [(s.start_datetime, s.end_datetime) for s in sleeps] == [(clean_start, end)]
    assert result["records_inserted"] == 2
    assert sleeps[0].sleep_detail.sleep_time_in_bed_minutes == (900 if wrapper == "in_bed" else 600)


def test_sleep_final_deferred_constraint_rejection_rolls_back_only_its_item(db: Session) -> None:
    member = UserFactory()
    history = WearableHistoryRepository().begin_source(db, member.id, "apple")
    own = DataSourceFactory(user=member, provider="apple")
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    history.freeze_at = datetime.now(UTC) + timedelta(seconds=1)
    db.commit()
    deadline = history.freeze_at
    sample_id = uuid4()
    db.execute(
        insert(DataPointSeries).values(
            id=sample_id,
            data_source_id=own.id,
            series_type_definition_id=get_series_type_id(SeriesType.heart_rate),
            recorded_at=day + timedelta(hours=12),
            value=61,
        )
    )
    start = day - timedelta(days=40, hours=4)
    record = EventRecordCreate(
        id=uuid4(),
        data_source_id=own.id,
        user_id=member.id,
        source="apple",
        category="sleep",
        type="sleep_session",
        source_name="watch",
        start_datetime=start,
        end_datetime=start + timedelta(hours=8),
        zone_offset="+00:00",
    )

    def cross_freeze(_record: EventRecord) -> None:
        while datetime.now(UTC) <= deadline:
            sleep(0.01)

    result = EventRecordService(getLogger(__name__)).create_or_merge_sleep(
        db,
        member.id,
        record,
        EventRecordDetailCreate(record_id=record.id, sleep_total_duration_minutes=480),
        60,
        before_commit=cross_freeze,
    )
    assert result is None
    db.commit()
    assert db.get(DataPointSeries, sample_id) is not None
    assert db.get(EventRecord, record.id) is None
    assert db.scalar(text("SELECT count(*) FROM sleep_details WHERE record_id=:r"), {"r": record.id}) == 0


def test_admitted_sleep_callback_failure_keeps_prior_work_and_rethrows(db: Session) -> None:
    member, history = frozen_source(db)
    own = DataSourceFactory(user=member, provider="apple")
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    db.commit()
    prior_id = uuid4()
    db.execute(
        insert(DataPointSeries).values(
            id=prior_id,
            data_source_id=own.id,
            series_type_definition_id=get_series_type_id(SeriesType.heart_rate),
            recorded_at=day + timedelta(hours=12),
            value=61,
        )
    )
    record = EventRecordCreate(
        id=uuid4(),
        data_source_id=own.id,
        user_id=member.id,
        source="apple",
        category="sleep",
        type="sleep_session",
        source_name="watch",
        start_datetime=day - timedelta(hours=4),
        end_datetime=day + timedelta(hours=4),
        zone_offset="+00:00",
    )

    def fail(_record: EventRecord) -> Never:
        raise RuntimeError("contributor failed")

    with pytest.raises(RuntimeError, match="contributor failed"):
        EventRecordService(getLogger(__name__)).create_or_merge_sleep(
            db,
            member.id,
            record,
            EventRecordDetailCreate(record_id=record.id, sleep_total_duration_minutes=480),
            60,
            before_commit=fail,
        )
    db.commit()
    assert db.get(DataPointSeries, prior_id) is not None
    assert db.get(EventRecord, record.id) is None


@pytest.mark.parametrize("frozen", [False, True])
def test_sleep_adjacency_keeps_imports_before_freeze_and_admitted_naps_after_it(db: Session, frozen: bool) -> None:
    member = UserFactory()
    history = WearableHistoryRepository().begin_source(db, member.id, "apple")
    own = DataSourceFactory(user=member, provider="apple", source="apple")
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    start = day + timedelta(hours=10) if frozen else day - timedelta(days=2, hours=-10)
    adjacent = EventRecordFactory(
        data_source=own,
        category="sleep",
        type="sleep_session",
        source_name="watch",
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        zone_offset="+00:00",
    )
    if frozen:
        history.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    found = EventRecordRepository(EventRecord).find_adjacent_sleep_record(
        db, member.id, start + timedelta(hours=1), start + timedelta(hours=2), 60, provider="apple"
    )
    assert found.id == adjacent.id


@pytest.mark.parametrize("wrapper", ["in_bed", "sleeping"])
@pytest.mark.parametrize("offload", [False, True])
def test_sdk_evening_wrapper_cannot_prove_an_evening_sample_for_a_morning_nap(
    db: Session, wrapper: str, offload: bool
) -> None:
    member, history = frozen_source(db)
    history.start_date = date(2026, 10, 13)
    db.flush()
    evening = datetime(2026, 10, 12, 20, tzinfo=UTC)
    morning = datetime(2026, 10, 13, 10, tzinfo=UTC)
    end = morning + timedelta(hours=1)
    raw = envelope(
        {
            "sleep": [
                {"stage": wrapper, "startDate": evening.isoformat(), "endDate": end.isoformat()},
                {"stage": "light", "startDate": morning.isoformat(), "endDate": end.isoformat()},
            ],
            "records": [
                {
                    "type": "HEART_RATE",
                    "value": 61,
                    "unit": "count",
                    "startDate": evening.isoformat(),
                    "endDate": evening.isoformat(),
                }
            ],
        }
    )
    with (
        patch.object(settings, "sdk_payload_s3_offload", offload),
        patch("app.api.routes.v1.sdk_sync.load_sleep_state", return_value=None),
        patch("app.api.routes.v1.sdk_sync.store_raw_payload") as archive,
        patch("app.api.routes.v1.sdk_sync.put_payload_to_s3") as s3,
        patch("app.api.routes.v1.sdk_sync.process_sdk_upload.delay") as queue,
    ):
        with pytest.raises(HTTPException) as failure:
            sync_sdk_data(str(member.id), raw, SimpleNamespace(auth_type="api_key"), db)
        assert failure.value.status_code == 503
    archive.assert_not_called()
    s3.assert_not_called()
    queue.assert_not_called()
