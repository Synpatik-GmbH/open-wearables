import importlib.util
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.utils.wearable_history_clock import BERLIN, candidate_start, freeze_at, recorded_night_date


@pytest.fixture(autouse=True)
def installed_history_migration(db: Session) -> None:
    """Apply the actual append-only migration to the private previous source-free schema."""
    path = Path(__file__).parents[2] / "migrations/versions/2026_10_21_0700-a29691004b01_wearable_history_sources.py"
    spec = importlib.util.spec_from_file_location("history_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db.execute(text("INSERT INTO \"user\"(id) VALUES ('11111111-1111-1111-1111-111111111111')"))
    db.execute(
        text("""
        INSERT INTO data_source(id,user_id,provider) VALUES
        ('11111111-1111-1111-1111-111111111112','11111111-1111-1111-1111-111111111111','apple');
        INSERT INTO event_record(id,data_source_id,category,source_name,start_datetime,end_datetime) VALUES
        ('11111111-1111-1111-1111-111111111113','11111111-1111-1111-1111-111111111112','sleep','legacy',
         '2026-09-01 20:00+00','2026-09-02 04:00+00');
        """)
    )
    context = MigrationContext.configure(db.connection())
    with Operations.context(context):
        module.downgrade()
        module.upgrade()


def test_migration_keeps_previous_member_and_does_not_backfill_a_source(db: Session) -> None:
    assert db.scalar(text("SELECT count(*) FROM \"user\" WHERE id='11111111-1111-1111-1111-111111111111'")) == 1
    assert (
        db.scalar(
            text("SELECT count(*) FROM wearable_history_sources WHERE user_id='11111111-1111-1111-1111-111111111111'")
        )
        == 0
    )
    assert (
        db.scalar(text("SELECT source_name FROM event_record WHERE id='11111111-1111-1111-1111-111111111113'"))
        == "legacy"
    )


CONNECTION = date(2026, 10, 13)


def days(*offsets: int) -> list[date]:
    return [CONNECTION + timedelta(days=n) for n in offsets]


def test_ac1_latest_gap_selects_last_island() -> None:
    assert candidate_start(CONNECTION, days(-24, *range(-8, 0))) == CONNECTION - timedelta(days=8)


def test_ac2_continuous_window_and_ac3_filled_gap_admit_first_night() -> None:
    assert candidate_start(CONNECTION, days(*range(-28, 1))) == CONNECTION - timedelta(days=28)
    assert candidate_start(CONNECTION, days(-24, *range(-23, 0))) == CONNECTION - timedelta(days=24)


@pytest.mark.parametrize("offsets", [[], [-4], [-24, -8, -7, -6, -5, -4, -3]])
def test_ac9_no_night_or_trailing_three_day_gap_uses_connection_day(offsets: list[int]) -> None:
    assert candidate_start(CONNECTION, days(*offsets)) == CONNECTION


def test_two_day_trailing_gap_and_exact_window_edges() -> None:
    assert candidate_start(CONNECTION, days(-29, -28, *range(-27, -1), 1)) == CONNECTION - timedelta(days=28)


def test_ac10_freeze_is_midnight_after_seventh_berlin_day_and_tracks_dst() -> None:
    assert freeze_at(CONNECTION) == datetime(2026, 10, 19, 22, tzinfo=UTC)
    assert freeze_at(date(2026, 10, 21)) == datetime(2026, 10, 27, 23, tzinfo=UTC)


@pytest.mark.parametrize(
    ("start", "end", "offset", "expected"),
    [
        ("2026-10-12T20:00:00+00:00", "2026-10-13T06:00:00+00:00", None, CONNECTION),
        ("2026-10-12T16:00:00+00:00", "2026-10-12T23:00:00+00:00", "+02:00", CONNECTION),
        ("2026-10-13T04:00:00+00:00", "2026-10-13T05:00:00+00:00", "+02:00", None),
        ("2026-10-13T01:00:00+00:00", "2026-10-13T01:00:00+00:00", None, None),
        (None, "2026-10-13T01:00:00+00:00", None, None),
    ],
)
def test_night_acceptance_reuses_adapter_rule(
    start: str | None, end: str, offset: str | None, expected: date | None
) -> None:
    assert (
        recorded_night_date(datetime.fromisoformat(start) if start else None, datetime.fromisoformat(end), offset)
        == expected
    )


def test_ac3_ac4_ac11_source_store_promotes_only_earlier_and_survives_reconnect(db: Session) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import UserFactory

    member = UserFactory()
    repository = WearableHistoryRepository()
    connected = datetime(2026, 10, 13, 10, tzinfo=UTC)
    source = repository.begin_source(db, member.id, "apple", connected)
    repository.observe_nights(db, member.id, "apple", days(-24, *range(-8, 0)), connected)
    assert source.start_date == CONNECTION - timedelta(days=8)
    repository.observe_nights(db, member.id, "apple", days(*range(-23, -8)), connected)
    assert source.start_date == CONNECTION - timedelta(days=24)
    repository.observe_nights(db, member.id, "apple", days(0), connected)
    assert source.start_date == CONNECTION - timedelta(days=24)
    reconnected = repository.begin_source(db, member.id, "apple", connected + timedelta(days=20))
    assert reconnected.id == source.id
    assert reconnected.connection_day == CONNECTION
    assert reconnected.night_dates == days(*range(-24, 1))


def test_ac10_exact_freeze_stops_evidence_and_ac12_sources_and_members_are_independent(db: Session) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import UserFactory

    member, other = UserFactory(), UserFactory()
    repository = WearableHistoryRepository()
    connected = datetime(2026, 10, 13, 10, tzinfo=UTC)
    apple = repository.begin_source(db, member.id, "apple", connected)
    polar = repository.begin_source(db, member.id, "polar", connected + timedelta(days=2))
    other_apple = repository.begin_source(db, other.id, "apple", connected)
    repository.observe_nights(db, member.id, "apple", days(*range(-28, 1)), apple.freeze_at)
    assert apple.start_date == CONNECTION
    assert apple.night_dates == []
    assert polar.connection_day == CONNECTION + timedelta(days=2)
    assert other_apple.start_date == CONNECTION


def test_ac19_member_erasure_cascades_source_store(db: Session) -> None:
    from sqlalchemy import select

    from app.models.wearable_history_source import WearableHistorySource
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import UserFactory

    member = UserFactory()
    email = member.email
    source = WearableHistoryRepository().begin_source(db, member.id, "apple", datetime.now(UTC))
    source_id = source.id
    db.delete(member)
    db.flush()
    db.expire_all()
    assert db.scalar(select(WearableHistorySource).where(WearableHistorySource.id == source_id)) is None
    replacement = UserFactory(email=email)
    connected = datetime.now(UTC) + timedelta(days=2)
    fresh = WearableHistoryRepository().begin_source(db, replacement.id, "apple", connected)
    assert fresh.id != source_id
    assert fresh.user_id == replacement.id
    assert fresh.connection_day == connected.astimezone(BERLIN).date()
    assert fresh.start_date == fresh.connection_day
    assert fresh.night_dates == []


def test_arriving_sleep_updates_source_before_provider_selection_and_is_retained(db: Session) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import DataSourceFactory, EventRecordFactory, UserFactory

    member = UserFactory()
    repository = WearableHistoryRepository()
    source = repository.begin_source(db, member.id, "polar")
    data_source = DataSourceFactory(user=member, provider="polar")
    connection = source.connection_day
    records = []
    for offset in [-24, *range(-8, 1)]:
        night = connection + timedelta(days=offset)
        records.append(
            EventRecordFactory(
                data_source=data_source,
                category="sleep",
                start_datetime=datetime.combine(night - timedelta(days=1), datetime.min.time(), UTC)
                + timedelta(hours=20),
                end_datetime=datetime.combine(night, datetime.min.time(), UTC) + timedelta(hours=6),
            )
        )
    db.flush()
    db.expire(source)
    assert source.start_date == connection - timedelta(days=8)
    assert len(records) == 10
    assert db.get(type(records[0]), records[0].id) is not None


@pytest.mark.parametrize("path", ["orm", "bulk"])
def test_night_evidence_preserves_p6_clock_rule_when_provider_nap_flag_disagrees(db: Session, path: str) -> None:
    from app.models import SleepDetails
    from app.repositories.event_record_detail_repository import EventRecordDetailRepository
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.schemas.model_crud.activities import EventRecordDetailCreate
    from tests.factories import DataSourceFactory, EventRecordFactory, SleepDetailsFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "polar")
    data_source = DataSourceFactory(user=member, provider="polar")
    connection = source.connection_day
    for offset, is_nap in [(-2, True), (-3, False), (-1, False), (0, False)]:
        night = connection + timedelta(days=offset)
        record = EventRecordFactory(
            data_source=data_source,
            category="sleep",
            start_datetime=datetime.combine(night - timedelta(days=1), datetime.min.time(), UTC)
            + timedelta(hours=12 if offset == -3 else 20),
            end_datetime=datetime.combine(night, datetime.min.time(), UTC) + timedelta(hours=4),
        )
        if offset == -2:
            assert source.start_date == connection - timedelta(days=2)
            assert source.night_dates == [connection - timedelta(days=2)]
        if path == "orm":
            SleepDetailsFactory(event_record=record, is_nap=is_nap)
        else:
            EventRecordDetailRepository(SleepDetails).bulk_create(
                db, [EventRecordDetailCreate(record_id=record.id, is_nap=is_nap)], detail_type="sleep"
            )
        db.flush()
    db.expire(source)
    assert source.start_date == connection - timedelta(days=2)
    assert source.night_dates == [connection - timedelta(days=2), connection - timedelta(days=1), connection]


def test_frozen_sdk_first_night_and_its_samples_import_in_one_batch_without_a_preexisting_event(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from sqlalchemy import select

    from app.integrations.celery.tasks.finalize_stale_sleep_task import finalize_stale_sleeps
    from app.models import DataPointSeries, DataSource, EventRecord
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.services.sdk.import_service import import_service
    from tests.factories import UserFactory

    monkeypatch.setattr(finalize_stale_sleeps, "delay", MagicMock())
    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    end = start + timedelta(hours=8)
    raw = {
        "provider": "apple",
        "sdkVersion": "1",
        "syncTimestamp": datetime.now(UTC).isoformat(),
        "data": {
            "sleep": [{"stage": "light", "startDate": start.isoformat(), "endDate": end.isoformat()}],
            "records": [
                dict(type="HEART_RATE", value=60, unit="count", startDate=d.isoformat(), endDate=d.isoformat())
                for d in [start, end]
            ],
        },
    }
    assert not list(db.scalars(select(EventRecord).join(DataSource).where(DataSource.user_id == member.id)))
    result = import_service.load_data(db, raw, str(member.id))
    assert result["sleep_saved"] == 1
    assert result["records_inserted"] == 2
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    sleeps = list(db.scalars(select(EventRecord).join(DataSource).where(DataSource.user_id == member.id)))
    assert [(r.start_datetime, r.end_datetime) for r in sleeps] == [(start, end)]
    samples = list(db.scalars(select(DataPointSeries).join(DataSource).where(DataSource.user_id == member.id)))
    assert sorted(s.recorded_at for s in samples) == [start, end]


def test_ac12_divergent_source_cutoffs_filter_ingress_and_reads_independently(db: Session) -> None:
    from uuid import uuid4

    from sqlalchemy import select

    from app.models import DataPointSeries
    from app.repositories.data_point_series_repository import DataPointSeriesRepository
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.schemas.enums import SeriesType, get_series_type_id
    from app.schemas.model_crud.activities import TimeSeriesSampleCreate
    from tests.factories import UserFactory

    member = UserFactory()
    history = WearableHistoryRepository()
    apple = history.begin_source(db, member.id, "apple")
    polar = history.begin_source(db, member.id, "polar")
    connection = apple.connection_day
    history.observe_nights(
        db, member.id, "polar", [connection - timedelta(days=n) for n in range(6)], datetime.now(UTC)
    )
    assert apple.start_date == connection
    assert polar.start_date == connection - timedelta(days=5)
    apple.freeze_at = polar.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    instant = datetime.combine(connection - timedelta(days=3), datetime.min.time(), UTC) + timedelta(hours=12)
    samples = [
        TimeSeriesSampleCreate(
            id=uuid4(),
            user_id=member.id,
            provider=p,
            source=p,
            recorded_at=instant,
            series_type=SeriesType.heart_rate,
            value=value,
        )
        for p, value in [("apple", 99), ("polar", 60)]
    ]
    repository = DataPointSeriesRepository(DataPointSeries)
    counts = repository.bulk_create(db, samples)
    db.flush()
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert counts.inserted == 1
    assert set(db.scalars(select(DataPointSeries.id).where(DataPointSeries.id.in_([s.id for s in samples])))) == {
        samples[1].id
    }
    assert repository.query_series(
        db,
        member.id,
        get_series_type_id(SeriesType.heart_rate),
        instant - timedelta(seconds=1),
        instant + timedelta(seconds=1),
    ) == [(instant, 60.0)]


@pytest.mark.parametrize("fail_first", [False, True])
def test_frozen_polar_first_night_keeps_hr_and_score_atomic_and_retryable(db: Session, fail_first: bool) -> None:
    from unittest.mock import patch

    from sqlalchemy import select

    from app.models import DataPointSeries, DataSource, EventRecord, HealthScore
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.services.providers.polar.strategy import PolarStrategy
    from tests.factories import UserFactory

    member = UserFactory()
    member_id = member.id
    source = WearableHistoryRepository().begin_source(db, member_id, "polar")
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    end = start + timedelta(hours=8)
    db.commit()
    raw = {
        "date": source.start_date.isoformat(),
        "sleep_start_time": start.isoformat(),
        "sleep_end_time": end.isoformat(),
        "light_sleep": 8 * 3600,
        "sleep_score": 80,
        "heart_rate_samples": {"20:00": 60, "04:00": 58},
    }
    service = PolarStrategy().data_247
    if fail_first:
        with (
            patch.object(service, "_save_timeseries", side_effect=RuntimeError("sample write unavailable")),
            pytest.raises(RuntimeError, match="sample write unavailable"),
        ):
            service._save_sleep_items(db, member_id, [raw])
        db.rollback()
        assert not list(db.scalars(select(EventRecord).join(DataSource).where(DataSource.user_id == member_id)))
        assert not list(db.scalars(select(HealthScore).where(HealthScore.user_id == member_id)))
    assert service._save_sleep_items(db, member_id, [raw]) == 1
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    sleeps = list(db.scalars(select(EventRecord).join(DataSource).where(DataSource.user_id == member_id)))
    assert [(r.start_datetime, r.end_datetime) for r in sleeps] == [(start, end)]
    samples = list(db.scalars(select(DataPointSeries).join(DataSource).where(DataSource.user_id == member_id)))
    assert sorted((s.recorded_at, int(s.value)) for s in samples) == [(start, 60), (end, 58)]
    provider_scores = list(
        db.scalars(select(HealthScore).where(HealthScore.user_id == member_id, HealthScore.provider == "polar"))
    )
    assert [(s.event_record_id, int(s.value)) for s in provider_scores] == [(sleeps[0].id, 80)]


@pytest.mark.parametrize("sink", ["raw-log", "raw-s3", "fit"])
def test_frozen_raw_archive_denial_reaches_each_storage_sink(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, sink: str
) -> None:
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    import app.database
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.services import raw_payload_storage
    from tests.factories import UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()

    @contextmanager
    def archive_session() -> Iterator[Session]:
        yield db

    client = MagicMock()
    monkeypatch.setattr(app.database, "SessionLocal", archive_session)
    monkeypatch.setattr(raw_payload_storage, "_storage_backend", "log" if sink == "raw-log" else "s3")
    monkeypatch.setattr(raw_payload_storage, "_s3_client", client)
    monkeypatch.setattr(raw_payload_storage, "_s3_bucket", "test-bucket")
    monkeypatch.setattr(raw_payload_storage, "_fit_files_enabled", True)
    if sink == "fit":
        raw_payload_storage.store_fit_file(
            provider="apple", user_id=str(member.id), fit_bytes=b"dated FIT", activity_id="first-night"
        )
    else:
        raw_payload_storage.store_raw_payload(
            source="sdk", provider="apple", user_id=str(member.id), payload={"old": 1}
        )
    assert capsys.readouterr().out == ""
    client.put_object.assert_not_called()


def test_ac7_frozen_orm_ingress_rejects_prestart_samples_and_preserves_weight(db: Session) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.utils.wearable_history_ingress import PreStartDataRejectedError
    from tests.factories import DataPointSeriesFactory, DataSourceFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    data_source = DataSourceFactory(user=member, provider="apple")
    with pytest.raises(PreStartDataRejectedError), db.begin_nested():
        DataPointSeriesFactory(data_source=data_source, recorded_at=datetime.now(UTC) - timedelta(days=2))


def test_ac7_bulk_ingress_keeps_first_night_endpoints_and_weight_but_rejects_older_samples(db: Session) -> None:
    from uuid import uuid4

    from sqlalchemy import select

    from app.models import DataPointSeries
    from app.repositories.data_point_series_repository import DataPointSeriesRepository
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.schemas.enums import SeriesType
    from app.schemas.model_crud.activities import TimeSeriesSampleCreate
    from tests.factories import DataSourceFactory, EventRecordFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    data_source = DataSourceFactory(user=member, provider="apple")
    start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    end = start + timedelta(hours=8)
    EventRecordFactory(data_source=data_source, category="sleep", start_datetime=start, end_datetime=end)
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    creators = [
        TimeSeriesSampleCreate(
            id=uuid4(),
            user_id=member.id,
            provider="apple",
            source="apple_health_sdk",
            recorded_at=instant,
            series_type=SeriesType(metric),
            value=60,
        )
        for metric, instant in [
            ("heart_rate", start),
            ("heart_rate", end),
            ("heart_rate", start - timedelta(days=1)),
            ("weight", start - timedelta(days=1)),
        ]
    ]
    counts = DataPointSeriesRepository(DataPointSeries).bulk_create(db, creators)
    db.flush()
    stored = set(db.scalars(select(DataPointSeries.id).where(DataPointSeries.id.in_([c.id for c in creators]))))
    assert stored == {creators[0].id, creators[1].id, creators[3].id}
    assert counts.inserted == 3


def test_frozen_sdk_payload_is_sanitized_before_queue_and_keeps_first_night_samples(db: Session) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.services.sdk.wearable_history import sanitize_sdk_payload
    from tests.factories import UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    end = start + timedelta(hours=8)
    old = start - timedelta(days=2)
    body = {
        "provider": "apple",
        "sdkVersion": "1",
        "syncTimestamp": datetime.now(UTC).isoformat(),
        "data": {
            "sleep": [
                {"stage": "light", "startDate": start.isoformat(), "endDate": (start + timedelta(hours=8)).isoformat()}
            ],
            "records": [
                dict(
                    id=metric,
                    type=metric,
                    value=60,
                    unit="count",
                    startDate=instant.isoformat(),
                    endDate=instant.isoformat(),
                )
                for metric, instant in [("HEART_RATE", start), ("STEP_COUNT", old), ("WEIGHT", old)]
            ],
            "workouts": [
                {"id": "held", "startDate": old.isoformat(), "endDate": (old + timedelta(minutes=20)).isoformat()},
                {
                    "id": "admitted",
                    "startDate": end.isoformat(),
                    "endDate": (end + timedelta(minutes=20)).isoformat(),
                    "samples": [{"timestamp": old.isoformat(), "type": "heartRate", "value": 60}],
                    "metadata": {"nestedRecords": [{"timestamp": old.isoformat()}]},
                },
            ],
        },
    }
    filtered = sanitize_sdk_payload(db, member.id, body)
    assert [sample["id"] for sample in filtered["data"]["records"]] == ["HEART_RATE", "WEIGHT"]
    assert len(filtered["data"]["sleep"]) == 1
    assert [workout["id"] for workout in filtered["data"]["workouts"]] == ["admitted"]
    assert filtered["data"]["workouts"][0]["samples"] is None
    assert filtered["data"]["workouts"][0]["metadata"] is None


def test_first_calendar_day_sample_inside_an_older_night_is_rejected_by_night_date(db: Session) -> None:
    from types import SimpleNamespace

    from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import DataSourceFactory, EventRecordFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    data_source = DataSourceFactory(user=member, provider="apple")
    start = datetime.combine(source.start_date - timedelta(days=5), datetime.min.time(), UTC) + timedelta(hours=3)
    end = datetime.combine(source.start_date, datetime.min.time(), UTC) + timedelta(hours=3)
    EventRecordFactory(data_source=data_source, category="sleep", start_datetime=start, end_datetime=end)
    EventRecordFactory(
        data_source=data_source,
        category="sleep",
        start_datetime=end + timedelta(minutes=30),
        end_datetime=end + timedelta(minutes=90),
    )
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    sample = SimpleNamespace(user_id=member.id, provider="apple", series_type="heart_rate", recorded_at=end)
    assert not WearableHistoryIngressRepository(db).sample_admitted(sample)


def test_raw_archive_admission_skips_frozen_and_unattributed_payloads_without_affecting_other_member(
    db: Session,
) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import UserFactory

    repository = WearableHistoryRepository()
    member, other = UserFactory(), UserFactory()
    source = repository.begin_source(db, member.id, "apple")
    assert repository.raw_archive_allowed(db, member.id, "apple")
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    assert not repository.raw_archive_allowed(db, member.id, "apple")
    assert not repository.raw_archive_allowed(db, None, "apple")
    assert repository.raw_archive_allowed(db, other.id, "apple")


def test_archival_does_not_create_a_mixed_prestart_aggregate_or_delete_its_contributors(db: Session) -> None:
    from sqlalchemy import select

    from app.models import DataPointSeries, DataPointSeriesArchive
    from app.repositories.archival_repository import DataPointSeriesArchiveRepository
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.utils.wearable_history_ingress import PreStartDataRejectedError
    from tests.factories import DataPointSeriesFactory, DataSourceFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    data_source = DataSourceFactory(user=member, provider="apple")
    sample = DataPointSeriesFactory(data_source=data_source, recorded_at=datetime.now(UTC) - timedelta(days=5))
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    with pytest.raises(PreStartDataRejectedError):
        DataPointSeriesArchiveRepository().archive_data_before(db, date.today())
    assert db.get(DataPointSeries, sample.id) is not None
    assert not list(
        db.scalars(select(DataPointSeriesArchive).where(DataPointSeriesArchive.data_source_id == data_source.id))
    )


def test_frozen_database_constraint_catches_core_insert_bypassing_repository_and_orm(db: Session) -> None:
    from uuid import uuid4

    from sqlalchemy import insert
    from sqlalchemy.exc import DBAPIError

    from app.models import EventRecord
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import DataSourceFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple", datetime.now(UTC) - timedelta(days=10))
    data_source = DataSourceFactory(user=member, provider="apple")
    old = datetime.combine(source.start_date - timedelta(days=2), datetime.min.time(), UTC) + timedelta(hours=12)
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(
            insert(EventRecord).values(
                id=uuid4(),
                data_source_id=data_source.id,
                category="workout",
                source_name="apple",
                start_datetime=old,
                end_datetime=old + timedelta(hours=1),
            )
        )


@pytest.mark.parametrize("kind", ["sample", "sleep_details", "workout_details", "provider_score"])
def test_deferred_database_constraints_reject_prestart_core_writes_at_commit_boundary(db: Session, kind: str) -> None:
    from uuid import uuid4

    from sqlalchemy import insert
    from sqlalchemy.exc import DBAPIError

    from app.models import DataPointSeries, HealthScore, SleepDetails, WorkoutDetails
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import DataSourceFactory, EventRecordFactory, UserFactory

    member = UserFactory()
    data_source = DataSourceFactory(user=member, provider="apple")
    old = datetime.now(UTC) - timedelta(days=15)
    parent = EventRecordFactory(
        data_source=data_source,
        category="sleep" if kind == "sleep_details" else "workout",
        start_datetime=old,
        end_datetime=old + timedelta(hours=1),
    )
    source = WearableHistoryRepository().begin_source(db, member.id, "apple", datetime.now(UTC) - timedelta(days=10))
    assert old.date() < source.start_date
    if kind == "sample":
        statement = insert(DataPointSeries).values(
            id=uuid4(), data_source_id=data_source.id, series_type_definition_id=1, recorded_at=old, value=60
        )
    elif kind == "provider_score":
        statement = insert(HealthScore).values(
            id=uuid4(),
            user_id=member.id,
            data_source_id=data_source.id,
            provider="apple",
            category="sleep",
            recorded_at=old,
            value=60,
        )
    else:
        statement = insert(SleepDetails if kind == "sleep_details" else WorkoutDetails).values(record_id=parent.id)
    savepoint = db.begin_nested()
    db.execute(statement)
    with pytest.raises(DBAPIError):
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    savepoint.rollback()


def test_frozen_first_night_and_both_endpoint_samples_commit_together(db: Session) -> None:
    from uuid import uuid4

    from sqlalchemy import insert, select

    from app.models import DataPointSeries, EventRecord
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import DataSourceFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple", datetime.now(UTC) - timedelta(days=10))
    data_source = DataSourceFactory(user=member, provider="apple")
    start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    end = start + timedelta(hours=8)
    ids = [uuid4(), uuid4()]
    with db.begin_nested():
        db.execute(
            insert(DataPointSeries),
            [
                dict(id=key, data_source_id=data_source.id, series_type_definition_id=1, recorded_at=instant, value=60)
                for key, instant in zip(ids, [start, end], strict=True)
            ],
        )
        db.execute(
            insert(EventRecord).values(
                id=uuid4(),
                data_source_id=data_source.id,
                category="sleep",
                source_name="apple",
                start_datetime=start,
                end_datetime=end,
            )
        )
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert set(db.scalars(select(DataPointSeries.id).where(DataPointSeries.id.in_(ids)))) == set(ids)


def test_provisional_baseline_reads_exclude_held_sources_and_keep_first_night_samples(db: Session) -> None:
    from app.models import DataPointSeries, EventRecord
    from app.repositories.data_point_series_repository import DataPointSeriesRepository
    from app.repositories.event_record_repository import EventRecordRepository
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.schemas.enums import SeriesType, get_series_type_id
    from tests.factories import DataPointSeriesFactory, DataSourceFactory, EventRecordFactory, UserFactory

    member = UserFactory()
    history = WearableHistoryRepository()
    source = history.begin_source(db, member.id, "apple")
    history.begin_source(db, member.id, "polar")
    apple = DataSourceFactory(user=member, provider="apple")
    polar = DataSourceFactory(user=member, provider="polar")
    start = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    end = start + timedelta(hours=8)
    first = EventRecordFactory(data_source=apple, category="sleep", start_datetime=start, end_datetime=end)
    EventRecordFactory(
        data_source=polar,
        category="sleep",
        start_datetime=start - timedelta(days=3),
        end_datetime=end - timedelta(days=3),
    )
    DataPointSeriesFactory(data_source=apple, recorded_at=start, value=70)
    DataPointSeriesFactory(data_source=apple, recorded_at=start - timedelta(days=3), value=50)
    DataPointSeriesFactory(data_source=polar, recorded_at=start - timedelta(days=3), value=99)
    # These are still stored for evidence; read eligibility applies while provisional.
    points = DataPointSeriesRepository(DataPointSeries).query_series(
        db, member.id, get_series_type_id(SeriesType.heart_rate), start - timedelta(days=3), end + timedelta(hours=1)
    )
    assert points == [(start, 70.0)]
    nights = EventRecordRepository(EventRecord).get_sleep_records_with_details(
        db, member.id, start - timedelta(days=3), end + timedelta(hours=1)
    )
    assert [record.id for record, _ in nights] == [first.id]


def test_internal_score_without_a_source_is_not_mistaken_for_a_new_wearable_connection(db: Session) -> None:
    from uuid import uuid4

    from sqlalchemy import select

    from app.models import HealthScore
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import UserFactory

    member = UserFactory()
    WearableHistoryRepository().begin_source(db, member.id, "apple", datetime.now(UTC) - timedelta(days=10))
    score = HealthScore(
        id=uuid4(),
        user_id=member.id,
        provider="internal",
        category="resilience",
        recorded_at=datetime.now(UTC),
        value=60,
    )
    db.add(score)
    db.flush()
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert db.scalar(select(HealthScore.value).where(HealthScore.id == score.id)) == 60


def test_provisional_sleep_score_does_not_score_a_held_record(db: Session) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.services.scores.sleep_service import sleep_score_service
    from tests.factories import DataSourceFactory, EventRecordFactory, SleepDetailsFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    data_source = DataSourceFactory(user=member, provider="apple")
    start = datetime.combine(source.start_date - timedelta(days=5), datetime.min.time(), UTC) + timedelta(hours=20)
    record = EventRecordFactory(
        data_source=data_source, category="sleep", start_datetime=start, end_datetime=start + timedelta(hours=8)
    )
    SleepDetailsFactory(event_record=record, is_nap=False, sleep_total_duration_minutes=480)
    db.expire(record, ["sleep_detail"])
    scores = sleep_score_service.get_sleep_scores_for_records(db, member.id, [(record.id, record.end_datetime.date())])
    assert scores == {}


def test_held_sleep_records_do_not_consume_the_score_reader_limit(db: Session) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.services.scores.sleep_service import sleep_score_service
    from tests.factories import DataSourceFactory, EventRecordFactory, SleepDetailsFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    data_source = DataSourceFactory(user=member, provider="apple")
    first = datetime.combine(source.start_date - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    for i in range(150):
        held = EventRecordFactory(
            data_source=data_source,
            category="sleep",
            start_datetime=first - timedelta(days=5) + timedelta(seconds=i),
            end_datetime=first - timedelta(days=5) + timedelta(hours=8, seconds=i),
        )
        SleepDetailsFactory(event_record=held, is_nap=False)
    admitted = EventRecordFactory(
        data_source=data_source, category="sleep", start_datetime=first, end_datetime=first + timedelta(hours=8)
    )
    SleepDetailsFactory(event_record=admitted, is_nap=False)
    db.expire(admitted, ["sleep_detail"])
    key = (admitted.id, admitted.end_datetime.date())
    assert key in sleep_score_service.get_sleep_scores_for_records(db, member.id, [key])


@pytest.mark.parametrize(
    "metric",
    [
        "heart_rate",
        "heart_rate_variability_sdnn",
        "heart_rate_variability_rmssd",
        "resting_heart_rate",
        "respiratory_rate",
        "oxygen_saturation",
        "skin_temperature",
        "steps",
    ],
)
def test_every_wearable_metric_rejects_frozen_prestart_bulk_ingress(db: Session, metric: str) -> None:
    from uuid import uuid4

    from app.models import DataPointSeries
    from app.repositories.data_point_series_repository import DataPointSeriesRepository
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.schemas.enums import SeriesType
    from app.schemas.model_crud.activities import TimeSeriesSampleCreate
    from tests.factories import UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    creator = TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=member.id,
        provider="apple",
        source="apple_health_sdk",
        recorded_at=datetime.now(UTC) - timedelta(days=2),
        series_type=SeriesType(metric),
        value=60,
    )
    counts = DataPointSeriesRepository(DataPointSeries).bulk_create(db, [creator])
    assert counts.inserted == 0
    assert db.get(DataPointSeries, creator.id) is None


def test_locked_source_refreshes_previously_loaded_state_before_merging_evidence(db: Session) -> None:
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import UserFactory

    member = UserFactory()
    repo = WearableHistoryRepository()
    source = repo.begin_source(db, member.id, "apple")
    start = source.connection_day - timedelta(days=8)
    latest = [source.connection_day - timedelta(days=i) for i in range(1, 9)]
    # A committed updater can change this row while a long-lived session retains its old identity.
    db.execute(
        text("UPDATE wearable_history_sources SET start_date=:start, night_dates=:nights WHERE id=:id"),
        {"start": start, "nights": latest, "id": source.id},
    )
    assert source.start_date != start
    merged = repo.observe_nights(db, member.id, "apple", [source.connection_day - timedelta(days=24)])
    assert merged.start_date == start
    assert set(latest).issubset(merged.night_dates)


def test_evidence_uses_database_clock_after_lock_even_if_ingress_cached_a_prefreeze_clock(db: Session) -> None:
    from app.models import EventRecord
    from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from tests.factories import DataSourceFactory, UserFactory

    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    data_source = DataSourceFactory(user=member, provider="apple")
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    gate = WearableHistoryIngressRepository(db)
    gate.source((member.id, "apple"))
    gate.now = source.freeze_at - timedelta(seconds=1)
    nights = [
        EventRecord(
            data_source_id=data_source.id,
            category="sleep",
            start_datetime=datetime.combine(source.connection_day - timedelta(days=i + 1), datetime.min.time(), UTC)
            + timedelta(hours=20),
            end_datetime=datetime.combine(source.connection_day - timedelta(days=i), datetime.min.time(), UTC)
            + timedelta(hours=6),
            zone_offset=None,
        )
        for i in (0, 1)
    ]
    gate.observe_events(nights)
    db.refresh(source)
    assert source.start_date == source.connection_day
    assert source.night_dates == []


def test_missing_source_metadata_is_retried_while_weight_remains_exempt(db: Session) -> None:
    from uuid import uuid4

    from app.models import DataPointSeries
    from app.repositories.data_point_series_repository import DataPointSeriesRepository
    from app.repositories.wearable_history_repository import WearableHistoryRepository
    from app.schemas.enums import SeriesType
    from app.schemas.model_crud.activities import TimeSeriesSampleCreate
    from app.utils.wearable_history_ingress import WearableHistoryMetadataUnavailableError
    from tests.factories import UserFactory

    member = UserFactory()
    WearableHistoryRepository().begin_source(db, member.id, "apple")
    sample = TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=member.id,
        provider="polar",
        source="polar",
        recorded_at=datetime.now(UTC),
        series_type=SeriesType.heart_rate,
        value=60,
    )
    repository = DataPointSeriesRepository(DataPointSeries)
    with pytest.raises(WearableHistoryMetadataUnavailableError):
        repository.bulk_create(db, [sample])
    assert db.get(DataPointSeries, sample.id) is None
    weight = sample.model_copy(update={"id": uuid4(), "series_type": SeriesType.weight})
    assert repository.bulk_create(db, [weight]).inserted == 1
    assert db.get(DataPointSeries, weight.id) is not None
