import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from logging import getLogger
from time import sleep
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import sentry_sdk
from fastapi import HTTPException
from sentry_sdk.integrations.fastapi import FastApiIntegration, _wrap_async_handler
from sentry_sdk.transport import Transport
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routes.v1.oauth import oauth_callback
from app.api.routes.v1.sdk_sync import sync_sdk_data
from app.config import settings
from app.constants.series_types.sdk import SleepPhase
from app.models import DataPointSeries, DataSource, EventRecord
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.model_crud.activities import EventRecordCreate, TimeSeriesSampleCreate
from app.schemas.providers.oura import OuraWorkoutJSON
from app.schemas.providers.polar import ExerciseJSON
from app.services.providers.oura.strategy import OuraStrategy
from app.services.providers.polar.strategy import PolarStrategy
from app.services.sdk.import_service import import_service
from app.services.sdk.sleep_service import _apply_transition, _create_new_sleep_state
from app.services.sdk.wearable_history import sanitize_sdk_payload
from app.services.timeseries_service import TimeSeriesService
from tests.factories import DataSourceFactory, EventRecordFactory, UserConnectionFactory, UserFactory
from tests.services.test_wearable_history_ingress_closure import frozen_source


def envelope(data: dict) -> dict:
    return {"provider": "apple", "sdkVersion": "1", "syncTimestamp": datetime.now(UTC).isoformat(), "data": data}


def test_real_single_item_rejection_keeps_prior_uncommitted_admitted_samples(db: Session) -> None:
    member, source = frozen_source(db)
    day = datetime.combine(source.start_date, datetime.min.time(), UTC)
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
    source_id, _ = repo._build_creation(db, record)
    db.commit()  # Connection and source exist before the current multi-part import.
    sample = DataPointSeries(
        id=uuid4(),
        data_source_id=source_id,
        series_type_definition_id=get_series_type_id(SeriesType.heart_rate),
        recorded_at=day + timedelta(hours=12),
        value=61,
    )
    db.add(sample)
    assert WearableHistoryIngressRepository(db).persist_event(record, lambda: repo.create_and_flush(db, record)) is None
    db.flush()
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert db.scalar(select(DataPointSeries.value).where(DataPointSeries.id == sample.id)) == 61
    assert db.get(EventRecord, record.id) is None


def test_real_polar_flush_loop_skips_frozen_history_and_keeps_its_three_row_transaction(db: Session) -> None:
    member, source = frozen_source(db, "polar")
    day = datetime.combine(source.start_date, datetime.min.time(), UTC)
    exercises = [
        ExerciseJSON(
            id=str(uuid4()),
            device="Vantage V3",
            sport="RUNNING",
            start_time=t.isoformat(),
            start_time_utc_offset=0,
            duration="PT1H",
            calories=100,
        )
        for t in [day - timedelta(days=2) + timedelta(hours=10), day + timedelta(hours=10), day + timedelta(hours=12)]
    ]
    strategy = PolarStrategy()
    assert strategy.workouts._save_exercises(db, member.id, exercises) == 2
    assert strategy.workouts._save_exercises(db, member.id, exercises) == 0
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    stored = list(db.scalars(select(EventRecord).join(DataSource).where(DataSource.user_id == member.id)))
    assert {e.external_id for e in stored} == {e.id for e in exercises[1:]}
    for table in ["workout_details", "polar_workout_true_time"]:
        column = "record_id" if table == "workout_details" else "event_record_id"
        assert (
            db.scalar(text(f"SELECT count(*) FROM {table} WHERE {column}=ANY(:ids)"), {"ids": [e.id for e in stored]})
            == 2
        )


@pytest.mark.parametrize("path", ["orm", "core", "wrapped"])
def test_single_item_crossing_freeze_is_skipped_and_the_next_record_persists(db: Session, path: str) -> None:
    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    data_source = DataSourceFactory(user=member, provider="apple")
    source.freeze_at = datetime.now(UTC) + timedelta(seconds=1)
    db.commit()
    deadline = source.freeze_at
    day = datetime.combine(source.start_date, datetime.min.time(), UTC)
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

    def write() -> EventRecord:
        while datetime.now(UTC) <= deadline:
            sleep(0.01)
        if path == "orm":
            return repo.create_and_flush(db, record)
        try:
            db.execute(
                insert(EventRecord).values(
                    id=record.id,
                    data_source_id=data_source.id,
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
    assert db.get(EventRecord, record.id) is None
    admitted = record.model_copy(
        update={"id": uuid4(), "start_datetime": day + timedelta(hours=12), "end_datetime": day + timedelta(hours=13)}
    )
    assert (
        WearableHistoryIngressRepository(db).persist_event(admitted, lambda: repo.create_and_flush(db, admitted))
        is not None
    )
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert db.get(EventRecord, admitted.id) is not None


def test_oauth_callback_anchors_history_to_the_persisted_pre_midnight_connection(db: Session) -> None:
    member = UserFactory()
    connected = datetime(2026, 10, 13, 21, 59, 59, tzinfo=UTC)
    db.add(UserConnectionFactory.build(user_id=member.id, provider="oura", created_at=connected))
    db.flush()
    strategy = OuraStrategy()
    with (
        patch("app.api.routes.v1.oauth.get_oauth_strategy", return_value=strategy),
        patch.object(
            strategy.oauth, "handle_callback", return_value=SimpleNamespace(user_id=member.id, redirect_uri=None)
        ),
        patch.object(settings, "historical_sync_on_connect", False),
    ):
        assert oauth_callback("oura", db, "code", "state", None, None).status_code == 303
    row = db.execute(
        text("SELECT connection_day, freeze_at FROM wearable_history_sources WHERE user_id=:u"), {"u": member.id}
    ).one()
    assert row.connection_day == date(2026, 10, 13)
    assert row.freeze_at == datetime(2026, 10, 19, 22, tzinfo=UTC)


@pytest.mark.parametrize("path", ["service", "template"])
def test_real_oura_loop_skips_a_frozen_item_without_starving_its_admitted_neighbours(db: Session, path: str) -> None:
    member, source = frozen_source(db, "oura")
    day = datetime.combine(source.start_date, datetime.min.time(), UTC)
    records = [
        OuraWorkoutJSON(
            id=str(uuid4()),
            activity="running",
            source="manual",
            intensity="moderate",
            day=t.date().isoformat(),
            start_datetime=t.isoformat(),
            end_datetime=(t + timedelta(hours=1)).isoformat(),
            calories=100,
        )
        for t in [day - timedelta(days=2) + timedelta(hours=10), day + timedelta(hours=10), day + timedelta(hours=12)]
    ]
    strategy = OuraStrategy()
    with patch.object(strategy.workouts, "get_workouts", return_value=records):
        if path == "service":
            assert strategy.workouts.load_data(db, member.id) == 2
            assert strategy.workouts.load_data(db, member.id) == 2  # Idempotent replay.
        else:
            strategy.workouts.process_workouts(db, member.id, day - timedelta(days=2), day + timedelta(days=1))
            strategy.workouts.process_workouts(db, member.id, day - timedelta(days=2), day + timedelta(days=1))
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    stored = list(db.scalars(select(EventRecord).join(DataSource).where(DataSource.user_id == member.id)))
    assert {e.external_id for e in stored} == {r.id for r in records[1:]}
    assert db.scalar(
        text("SELECT count(*) FROM workout_details WHERE record_id=ANY(:ids)"), {"ids": [e.id for e in stored]}
    ) == (2 if path == "service" else 0)


@pytest.mark.parametrize(
    "metric",
    [
        SeriesType.heart_rate,
        SeriesType.heart_rate_variability_sdnn,
        SeriesType.heart_rate_variability_rmssd,
        SeriesType.resting_heart_rate,
        SeriesType.respiratory_rate,
        SeriesType.oxygen_saturation,
        # skin and body temperature are never stored (R-2026-10-23-f, R-2026-10-26-d).
    ],
)
def test_cross_midnight_daytime_nap_does_not_override_an_on_cutoff_samples_calendar_date(
    db: Session, metric: SeriesType
) -> None:
    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    day = datetime.combine(source.start_date, datetime.min.time(), UTC)
    data_source = DataSourceFactory(user=member, provider="apple")
    nap = EventRecordFactory(
        data_source=data_source,
        category="sleep",
        start_datetime=day - timedelta(hours=10),
        end_datetime=day + timedelta(hours=12),
    )
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    sample = TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=member.id,
        source="apple",
        recorded_at=day + timedelta(hours=11),
        value=61,
        series_type=metric,
    )
    gate = WearableHistoryIngressRepository(db)
    assert gate.filter_events([nap]) == []
    assert DataPointSeriesRepository(DataPointSeries).bulk_create(db, [sample]) == 1
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    persisted = db.scalar(
        select(DataPointSeries)
        .join(DataSource)
        .where(DataSource.user_id == member.id, DataPointSeries.series_type_definition_id == get_series_type_id(metric))
    )
    assert persisted.recorded_at == day + timedelta(hours=11)
    assert float(persisted.value) == 61
    aggregates = DataPointSeriesRepository(DataPointSeries).get_aggregates_for_period(
        db, member.id, day, day + timedelta(days=1), [metric]
    )[metric]
    assert (aggregates["avg"], aggregates["count"]) == (61, 1)


@pytest.mark.parametrize("existing_state", [False, True])
@pytest.mark.parametrize("offload", [False, True])
@pytest.mark.parametrize("admitted", [False, True])
def test_later_offset_matches_importer_before_sdk_transport_and_persistence(
    db: Session, existing_state: bool, offload: bool, admitted: bool
) -> None:
    member, source = frozen_source(db)
    source.start_date = date(2026, 10, 13)
    db.flush()
    start = datetime(2026, 10, 12, 15, 30, tzinfo=UTC) if admitted else datetime(2026, 10, 12, 20, tzinfo=UTC)
    middle, end = start + timedelta(hours=1), start + timedelta(hours=2)
    offset = "+04:00" if admitted else "-04:00"
    first = {"stage": "light", "startDate": start.isoformat(), "endDate": middle.isoformat()}
    later = {"stage": "light", "startDate": middle.isoformat(), "endDate": end.isoformat(), "zoneOffset": offset}
    state = None
    if existing_state:
        state = _create_new_sleep_state(start, middle, provider="apple")
        state = _apply_transition(db, str(member.id), state, SleepPhase.ASLEEP_LIGHT, start, middle, "apple")
    raw = envelope({"sleep": [later] if existing_state else [first, later]})
    if admitted:
        raw["data"]["records"] = [
            {
                "type": "HEART_RATE",
                "value": 60 + i,
                "unit": "count",
                "startDate": t.isoformat(),
                "endDate": t.isoformat(),
            }
            for i, t in enumerate([start, middle, end])
        ]
    with (
        patch.object(settings, "sdk_payload_s3_offload", offload),
        patch("app.api.routes.v1.sdk_sync.load_sleep_state", return_value=state),
        patch("app.api.routes.v1.sdk_sync.store_raw_payload") as archive,
        patch("app.api.routes.v1.sdk_sync.put_payload_to_s3", return_value="s3://test-bucket/clean") as s3,
        patch("app.api.routes.v1.sdk_sync.process_sdk_upload.delay") as queue,
    ):
        response = sync_sdk_data(str(member.id), raw, SimpleNamespace(auth_type="api_key"), db)
    assert response.status_code == 202
    stored = json.loads(s3.call_args.kwargs["payload"] if offload else queue.call_args.kwargs["content"])
    if not offload:
        assert json.loads(archive.call_args.kwargs["payload"]) == stored
    assert len(stored["data"]["sleep"]) == ((1 if existing_state else 2) if admitted else 0)
    assert len(stored["data"]["records"]) == (3 if admitted else 0)
    with (
        # import_service reads the state for the history gate; sleep_service reads it to merge.
        patch("app.services.sdk.import_service.load_sleep_state", return_value=state),
        patch("app.services.sdk.sleep_service.load_sleep_state", return_value=state),
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps.delay"),
    ):
        result = import_service.load_data(db, stored, str(member.id))
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    sleeps = list(db.scalars(select(EventRecord).join(DataSource).where(DataSource.user_id == member.id)))
    assert len(sleeps) == int(admitted)
    if admitted:
        assert (sleeps[0].start_datetime, sleeps[0].end_datetime, sleeps[0].zone_offset) == (start, end, offset)
        assert result["records_inserted"] == 3
        samples = list(db.scalars(select(DataPointSeries).join(DataSource).where(DataSource.user_id == member.id)))
        assert {s.recorded_at for s in samples} == {start, middle, end}
        assert {float(s.value) for s in samples} == {60, 61, 62}


def test_frozen_validation_warning_does_not_capture_original_fastapi_request(db: Session) -> None:
    member, source = frozen_source(db)
    old = datetime.combine(source.start_date - timedelta(days=10), datetime.min.time(), UTC)
    admitted = old + timedelta(days=10, hours=12)
    raw = envelope(
        {
            "records": [
                {
                    "type": "HEART_RATE",
                    "value": "private-rejected-health-value",
                    "unit": "count",
                    "startDate": old.isoformat(),
                    "endDate": old.isoformat(),
                },
                {
                    "type": "STEP_COUNT",
                    "value": 42,
                    "unit": "count",
                    "startDate": admitted.isoformat(),
                    "endDate": admitted.isoformat(),
                },
            ]
        }
    )
    raw["data"]["records"] = [raw["data"]["records"][0].copy() for _ in range(25)] + raw["data"]["records"][1:]
    captured: list[dict[str, Any]] = []

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope: Any) -> None:
            for item in envelope.items:
                if item.type == "event":
                    captured.append(item.payload.json)

    client = sentry_sdk.Client(
        dsn="https://review-only@example.invalid/1",
        transport=MemoryTransport,
        default_integrations=False,
        integrations=[FastApiIntegration()],
        traces_sample_rate=0,
    )
    body = json.dumps(raw).encode()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/test-only",
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            "server": ("example.invalid", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )
    request._body = body

    async def actual_handler(req: Request) -> dict:
        return sanitize_sdk_payload(db, member.id, await req.json())

    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            sanitized = asyncio.run(_wrap_async_handler(actual_handler, request))
    finally:
        client.close()
    assert len(sanitized["data"]["records"]) == 1
    assert len(captured) == 1
    assert captured[0]["contexts"]["dropped_records"]["dropped_count"] == 25
    assert len(captured[0]["contexts"]["dropped_records"]["errors"]) == 20
    assert "private-rejected-health-value" not in json.dumps(captured)
    assert "request" not in captured[0]


@pytest.mark.parametrize(
    ("first_offset", "duplicate_offset", "admitted"), [(None, "+04:00", False), ("+04:00", "-04:00", True)]
)
def test_duplicate_sleep_offset_uses_importer_first_stage_identity(
    db: Session, first_offset: str | None, duplicate_offset: str, admitted: bool
) -> None:
    member, source = frozen_source(db)
    source.start_date = date(2026, 10, 13)
    db.flush()
    start, end = datetime(2026, 10, 12, 15, 30, tzinfo=UTC), datetime(2026, 10, 12, 17, 30, tzinfo=UTC)
    stages = [
        {"stage": "light", "startDate": start.isoformat(), "endDate": end.isoformat(), "zoneOffset": offset}
        for offset in (first_offset, duplicate_offset)
    ]
    sanitized = sanitize_sdk_payload(db, member.id, envelope({"sleep": stages}))
    assert len(sanitized["data"]["sleep"]) == int(admitted)
    with patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps.delay"):
        import_service.load_data(db, sanitized, str(member.id))
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    sleeps = list(db.scalars(select(EventRecord).join(DataSource).where(DataSource.user_id == member.id)))
    assert len(sleeps) == int(admitted)
    if admitted:
        assert sleeps[0].zone_offset == first_offset


def test_prefetch_range_expands_for_an_unprimed_persisted_first_night(db: Session) -> None:
    member, source = frozen_source(db)
    start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    ds = DataSourceFactory(user=member, provider="apple")
    EventRecordFactory(data_source=ds, category="sleep", start_datetime=start, end_datetime=start + timedelta(hours=8))
    sample = SimpleNamespace(
        user_id=member.id, provider="apple", series_type="heart_rate", recorded_at=start + timedelta(days=2)
    )
    gate = WearableHistoryIngressRepository(db)
    gate.prime_samples([sample])
    sample.recorded_at = start
    assert gate.sample_admitted(sample)


@pytest.mark.parametrize(
    "metric",
    [
        "HEART_RATE",
        "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
        "HEART_RATE_VARIABILITY",
        "RESTING_HEART_RATE",
        "RESPIRATORY_RATE",
        "OXYGEN_SATURATION",
        # AppleSleepingWristTemperature is body temperature, never stored (R-2026-10-26-d).
    ],
)
def test_unresolved_first_night_sdk_sample_is_dropped_then_kept_once_its_night_is_stored(
    db: Session, metric: str
) -> None:
    member, source = frozen_source(db)
    start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    end = start + timedelta(hours=8)
    raw = envelope(
        {
            "records": [
                {
                    "type": metric,
                    "value": 60,
                    "unit": "count",
                    "startDate": start.isoformat(),
                    "endDate": start.isoformat(),
                }
            ]
        }
    )
    with (
        patch("app.api.routes.v1.sdk_sync.load_sleep_state", return_value=None),
        patch("app.api.routes.v1.sdk_sync.store_raw_payload"),
        patch("app.api.routes.v1.sdk_sync.put_payload_to_s3"),
        patch("app.api.routes.v1.sdk_sync.process_sdk_upload.delay") as queue,
        patch.object(settings, "sdk_payload_s3_offload", False),
    ):
        # Dropped, not retried: the upload succeeds without it, so the app's sync moves on.
        response = sync_sdk_data(str(member.id), json.loads(json.dumps(raw)), SimpleNamespace(auth_type="api_key"), db)
        assert response.status_code == 202
        assert json.loads(queue.call_args.kwargs["content"])["data"]["records"] == []
        ds = DataSourceFactory(user=member, provider="apple")
        EventRecordFactory(data_source=ds, category="sleep", start_datetime=start, end_datetime=end)
        response = sync_sdk_data(str(member.id), raw, SimpleNamespace(auth_type="api_key"), db)
    assert response.status_code == 202
    stored = json.loads(queue.call_args.kwargs["content"])
    assert len(stored["data"]["records"]) == 1
    assert stored["data"]["records"][0]["startDate"].startswith(start.isoformat()[:19])
    result = import_service.load_data(db, stored, str(member.id))
    assert result["records_inserted"] == 1
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert (
        db.scalar(select(DataPointSeries).join(DataSource).where(DataSource.user_id == member.id)).recorded_at == start
    )


@pytest.mark.parametrize(
    "metric",
    [
        "heart_rate",
        "heart_rate_variability_sdnn",
        "heart_rate_variability_rmssd",
        # skin and body temperature are never stored (R-2026-10-23-f, R-2026-10-26-d).
        "resting_heart_rate",
        "respiratory_rate",
        "oxygen_saturation",
    ],
)
def test_every_physiology_family_needs_its_stored_first_night_and_keeps_inclusive_endpoints(
    db: Session, metric: str
) -> None:
    member, source = frozen_source(db)
    start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    end = start + timedelta(hours=8)
    gate = WearableHistoryIngressRepository(db)
    sample = SimpleNamespace(user_id=member.id, provider="apple", series_type=metric, recorded_at=start)
    assert gate.sample_admitted(sample) is False
    ds = DataSourceFactory(user=member, provider="apple")
    EventRecordFactory(data_source=ds, category="sleep", start_datetime=start, end_datetime=end)
    creators = [
        TimeSeriesSampleCreate(
            id=uuid4(),
            user_id=member.id,
            provider="apple",
            source="apple",
            recorded_at=t,
            series_type=SeriesType(metric),
            value=61,
        )
        for t in [start, end]
    ]
    counts = DataPointSeriesRepository(DataPointSeries).bulk_create(db, creators)
    assert counts.inserted == 2
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert {
        s.recorded_at
        for s in db.scalars(select(DataPointSeries).join(DataSource).where(DataSource.user_id == member.id))
    } == {start, end}


@pytest.mark.parametrize("path", ["service", "bulk", "orm", "sdk"])
def test_interval_queries_are_bounded_by_sources_and_preserve_batch_admission(db: Session, path: str) -> None:
    samples = []
    expected_ids = set()
    members = []
    for provider in ["apple"] if path == "sdk" else ["apple", "polar"]:
        member, source = frozen_source(db, provider)
        members.append(member)
        ds = DataSourceFactory(user=member, provider=provider)
        start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
        end = start + timedelta(hours=8)
        EventRecordFactory(data_source=ds, category="sleep", start_datetime=start, end_datetime=end)
        for i in range(100):
            identity = uuid4()
            expected_ids.add(identity)
            instant = start + timedelta(minutes=i)
            if path == "orm":
                samples.append(
                    DataPointSeries(
                        id=identity,
                        data_source_id=ds.id,
                        series_type_definition_id=get_series_type_id(SeriesType.heart_rate),
                        recorded_at=instant,
                        value=60,
                    )
                )
            else:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=identity,
                        user_id=member.id,
                        provider=provider,
                        source=provider,
                        recorded_at=instant,
                        series_type=SeriesType.heart_rate,
                        value=60,
                    )
                )
    queries = []

    def record_query(_conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "FROM event_record JOIN data_source" in statement:
            queries.append(statement)

    connection = db.connection()
    event.listen(connection, "before_cursor_execute", record_query)
    try:
        if path == "orm":
            db.add_all(samples)
            db.flush()
        elif path == "sdk":
            raw = envelope(
                {
                    "records": [
                        {
                            "id": str(s.id),
                            "type": "HEART_RATE",
                            "value": 60,
                            "unit": "count",
                            "startDate": s.recorded_at.isoformat(),
                            "endDate": s.recorded_at.isoformat(),
                        }
                        for s in samples
                    ]
                }
            )
            sanitized = sanitize_sdk_payload(db, members[0].id, raw)
            assert len(sanitized["data"]["records"]) == 100
        elif path == "service":
            assert TimeSeriesService(getLogger(__name__)).bulk_create_samples(db, samples).inserted == 200
        else:
            assert DataPointSeriesRepository(DataPointSeries).bulk_create(db, samples).inserted == 200
    finally:
        event.remove(connection, "before_cursor_execute", record_query)
    assert 0 < len(queries) <= (4 if path == "service" else len(members))
    if path != "sdk":
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        persisted = list(db.scalars(select(DataPointSeries).where(DataPointSeries.id.in_(expected_ids))))
        assert {s.id for s in persisted} == expected_ids
        assert {float(s.value) for s in persisted} == {60}
