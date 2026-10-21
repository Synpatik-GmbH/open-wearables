from datetime import UTC, date, datetime, timedelta
from logging import getLogger
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    DataPointSeries,
    DataPointSeriesArchive,
    DataSource,
    EventRecord,
    HealthScore,
    ProviderPriority,
    User,
    WearableHistorySource,
    WorkoutDetails,
)
from app.repositories.archival_repository import DataPointSeriesArchiveRepository
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.health_score_repository import HealthScoreRepository
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import HealthScoreCategory, SeriesType, get_series_type_id
from app.services.summaries_service import SummariesService
from tests.factories import (
    DataSourceFactory,
    EventRecordFactory,
    HealthScoreFactory,
    UserFactory,
)


def summary_inputs(db: Session) -> tuple[User, WearableHistorySource, WearableHistorySource, DataSource, EventRecord]:
    member = UserFactory()
    repository = WearableHistoryRepository()
    apple = repository.begin_source(db, member.id, "apple")
    polar = repository.begin_source(db, member.id, "polar")
    apple.start_date, polar.start_date = date(2026, 10, 13), date(2026, 10, 15)
    a = DataSourceFactory(user=member, provider="apple", source="watch")
    p = DataSourceFactory(user=member, provider="polar", source="polar")
    for provider, priority in [("polar", 1), ("apple", 2)]:
        row = db.scalar(select(ProviderPriority).where(ProviderPriority.provider == provider))
        if row is None:
            db.add(ProviderPriority(id=uuid4(), provider=provider, priority=priority, updated_at=datetime.now(UTC)))
        else:
            row.priority = priority
    onset, wake = datetime(2026, 10, 12, 20, tzinfo=UTC), datetime(2026, 10, 13, 6, tzinfo=UTC)
    admitted = EventRecordFactory(data_source=a, category="sleep", start_datetime=onset, end_datetime=wake)
    EventRecordFactory(
        data_source=a, category="sleep", start_datetime=onset - timedelta(days=1), end_datetime=wake - timedelta(days=1)
    )
    EventRecordFactory(data_source=p, category="sleep", start_datetime=onset, end_datetime=wake)
    for source, instant, value, metric in [
        (a, onset - timedelta(hours=8), 99, SeriesType.heart_rate),
        (p, onset, 99, SeriesType.heart_rate),
        (a, onset, 60, SeriesType.heart_rate),
        (a, wake, 62, SeriesType.heart_rate),
        (a, onset - timedelta(hours=8), 99, SeriesType.steps),
        (a, wake + timedelta(hours=6), 10, SeriesType.steps),
        (p, wake + timedelta(hours=7), 99, SeriesType.body_temperature),
        (p, wake + timedelta(hours=6), 99, SeriesType.body_temperature),
        (a, wake + timedelta(hours=6), 37, SeriesType.body_temperature),
        (a, onset - timedelta(hours=8), 72, SeriesType.weight),
    ]:
        db.add(
            DataPointSeries(
                id=uuid4(),
                data_source_id=source.id,
                recorded_at=instant,
                value=value,
                series_type_definition_id=get_series_type_id(metric),
            )
        )
    for source, instant, value, parent in [
        (a, wake - timedelta(days=1), 99, None),
        (p, wake, 99, None),
        (p, wake + timedelta(days=1), 99, None),
        (a, wake, 50, admitted.id),
    ]:
        HealthScoreFactory(
            user_id=member.id,
            data_source=source,
            provider=source.provider,
            category="recovery",
            recorded_at=instant,
            value=value,
            event_record_id=parent,
        )
    db.flush()
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return member, apple, polar, a, admitted


START, END = datetime(2026, 10, 11, tzinfo=UTC), datetime(2026, 10, 16, tzinfo=UTC)


def test_workout_summary_rejects_by_start_day_before_grouping_cross_midnight_workouts(db: Session) -> None:
    member, _, _, source, _ = summary_inputs(db)
    for onset, value in [(datetime(2026, 10, 12, 20, tzinfo=UTC), 99), (datetime(2026, 10, 13, 10, tzinfo=UTC), 10)]:
        record = EventRecordFactory(
            data_source=source,
            category="workout",
            start_datetime=onset,
            end_datetime=datetime(2026, 10, 13, 12, tzinfo=UTC),
        )
        db.add(WorkoutDetails(record_id=record.id, energy_burned=value, distance=value, total_elevation_gain=value))
    db.flush()
    rows = EventRecordRepository(EventRecord).get_daily_workout_aggregates(db, member.id, START, END)
    assert len(rows) == 1
    assert rows[0]["energy_burned_kcal"] == 10
    assert rows[0]["distance_meters"] == 10
    assert rows[0]["elevation_meters"] == 10


def test_unproved_legacy_archives_follow_each_provisional_source_before_grouping(db: Session) -> None:
    member = UserFactory()
    sources = {p: DataSourceFactory(user=member, provider=p) for p in ["apple", "polar"]}
    for provider, day, value in [("apple", 12, 99), ("apple", 13, 10), ("polar", 13, 99)]:
        db.add(
            DataPointSeriesArchive(
                id=uuid4(),
                data_source_id=sources[provider].id,
                series_type_definition_id=get_series_type_id(SeriesType.steps),
                bucket_start_at=datetime(2026, 10, day, tzinfo=UTC),
                aggregation_type="sum",
                value=value,
                sample_count=1,
            )
        )
    db.flush()
    repository = WearableHistoryRepository()
    for provider, day in [("apple", 13), ("polar", 15)]:
        source = repository.begin_source(db, member.id, provider)
        source.start_date = date(2026, 10, day)
    db.flush()
    rows = DataPointSeriesArchiveRepository().get_daily_activity_aggregates_from_archive(
        db, member.id, START, END, [get_series_type_id(SeriesType.steps)]
    )
    assert len(rows) == 1
    assert rows[0]["provider"] == "apple"
    assert rows[0]["steps_sum"] == 10
    assert DataPointSeriesArchiveRepository().get_user_counts_by_provider_and_type_from_archive(
        db, member.id, START, END
    ) == [("apple", "steps", 1)]


@pytest.mark.parametrize("reader", ["activity", "active", "intensity", "latest", "period", "window"])
def test_every_series_summary_filters_before_aggregation_and_latest_selection(db: Session, reader: str) -> None:
    member, *_ = summary_inputs(db)
    repo = DataPointSeriesRepository(DataPointSeries)
    if reader == "activity":
        rows = repo.get_daily_activity_aggregates(db, member.id, START, END)
        assert all(r["provider"] != "polar" for r in rows)
        assert sum(r["steps_sum"] for r in rows) == 10
        assert next(r for r in rows if r["activity_date"] == date(2026, 10, 12))["hr_avg"] == 60
    elif reader == "active":
        rows = repo.get_daily_active_minutes(db, member.id, START, END, active_threshold=0)
        assert [r["activity_date"] for r in rows] == [date(2026, 10, 13)]
        assert rows[0]["active_minutes"] == 1
    elif reader == "intensity":
        rows = repo.get_daily_intensity_minutes(db, member.id, START, END, 50, 70, 80, 100)
        assert all(r["source"] != "polar" for r in rows)
        assert sum(r["light_minutes"] for r in rows) == 2
        assert sum(r["vigorous_minutes"] for r in rows) == 0
    elif reader == "latest":
        values = repo.get_latest_values_for_types(db, member.id, END, [SeriesType.body_temperature, SeriesType.weight])
        assert values[SeriesType.body_temperature][0] == 37
        assert values[SeriesType.weight][0] == 72
    elif reader == "period":
        value = repo.get_aggregates_for_period(db, member.id, START, END, [SeriesType.heart_rate])[
            SeriesType.heart_rate
        ]
        assert (value["avg"], value["count"], value["min"], value["max"]) == (61, 2, 60, 62)
    else:
        assert repo.get_latest_reading_within_window(db, member.id, SeriesType.body_temperature, START, END)[0] == 37


@pytest.mark.parametrize(
    "metric",
    [
        SeriesType.heart_rate,
        SeriesType.heart_rate_variability_sdnn,
        SeriesType.heart_rate_variability_rmssd,
        SeriesType.resting_heart_rate,
        SeriesType.respiratory_rate,
        SeriesType.oxygen_saturation,
        SeriesType.skin_temperature,
        SeriesType.body_temperature,
    ],
)
def test_summary_keeps_both_first_night_endpoints_for_every_physiology_family(db: Session, metric: SeriesType) -> None:
    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, "apple")
    source.start_date = date(2026, 10, 13)
    data_source = DataSourceFactory(user=member, provider="apple")
    onset, wake = datetime(2026, 10, 12, 20, tzinfo=UTC), datetime(2026, 10, 13, 6, tzinfo=UTC)
    EventRecordFactory(data_source=data_source, category="sleep", start_datetime=onset, end_datetime=wake)
    for instant, value in [(onset, 60), (wake, 62)]:
        db.add(
            DataPointSeries(
                id=uuid4(),
                data_source_id=data_source.id,
                recorded_at=instant,
                value=value,
                series_type_definition_id=get_series_type_id(metric),
            )
        )
    db.flush()
    result = DataPointSeriesRepository(DataPointSeries).get_aggregates_for_period(db, member.id, START, END, [metric])[
        metric
    ]
    assert (result["avg"], result["count"]) == (61, 2)


def test_sleep_and_recovery_summaries_filter_before_cursor_and_session_grouping(db: Session) -> None:
    member, _, _, _, admitted = summary_inputs(db)
    repo = EventRecordRepository(EventRecord)
    rows = repo.get_sleep_summaries(db, member.id, START, END, None, 1)
    assert len(rows) == 1
    assert rows[0]["record_id"] == admitted.id
    assert rows[0]["avg_hr"] == 60  # Existing summary uses a half-open window; arithmetic is unchanged.
    sessions = repo._get_sleep_sessions(db, member.id, START, END)
    assert sum(len(v) for v in sessions.values()) == 1
    scores = HealthScoreRepository(HealthScore).get_recovery_summaries(db, member.id, START, END, None, 1)
    assert [s["recovery_score"] for s in scores] == [50]
    assert [float(s.value) for s in HealthScoreRepository(HealthScore).get_latest_per_category(db, member.id)] == [50]
    assert (
        float(
            HealthScoreRepository(HealthScore).get_latest_by_category(db, member.id, HealthScoreCategory.RECOVERY).value
        )
        == 50
    )
    service = SummariesService(getLogger(__name__))
    assert len(service.get_sleep_summaries(db, member.id, START, END, None, 1).data) == 1
    assert len(service.get_recovery_summaries(db, member.id, START, END, None, 1).data) == 1


def test_archival_keeps_provisional_raw_and_freezes_only_admitted_contributors(db: Session) -> None:
    member, apple, polar, source, _ = summary_inputs(db)
    archive = DataPointSeriesArchiveRepository()
    archive.archive_data_before(db, END.date())
    assert (
        db.scalar(
            select(DataPointSeries).where(DataPointSeries.data_source_id == source.id, DataPointSeries.value == 99)
        )
        is not None
    )
    assert (
        db.scalar(
            select(DataPointSeriesArchive).where(
                DataPointSeriesArchive.data_source_id == source.id,
                DataPointSeriesArchive.series_type_definition_id == get_series_type_id(SeriesType.heart_rate),
            )
        )
        is None
    )
    apple.freeze_at = polar.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    archive.archive_data_before(db, END.date())
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    raw = list(db.scalars(select(DataPointSeries).where(DataPointSeries.data_source_id == source.id)))
    assert {float(s.value) for s in raw} == {
        99,
        60,
    }  # Mixed bucket is retained, including its admitted first-night sample.
    clean = DataSourceFactory(user=member, provider="apple", source="clean-night")
    db.add(
        DataPointSeries(
            id=uuid4(),
            data_source_id=clean.id,
            recorded_at=datetime(2026, 10, 12, 20, tzinfo=UTC),
            value=60,
            series_type_definition_id=get_series_type_id(SeriesType.heart_rate),
        )
    )
    db.flush()
    archive.archive_data_before(db, END.date())
    rows = archive.get_daily_activity_aggregates_from_archive(
        db, member.id, START, END, [get_series_type_id(SeriesType.heart_rate), get_series_type_id(SeriesType.steps)]
    )
    assert all(r["provider"] != "polar" for r in rows)
    assert next(r for r in rows if r["activity_date"] == date(2026, 10, 12))["hr_avg"] == 60
    assert sum(r["steps_sum"] for r in rows) == 10
