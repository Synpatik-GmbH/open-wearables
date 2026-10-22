"""2.96.9.2 D-01 and D-03: a frozen source's pre-start rows are purged, and nothing else."""

from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.integrations.celery.core import create_celery
from app.integrations.celery.tasks.wearable_history_purge_task import purge_frozen_sources_task
from app.models import (
    DataPointSeries,
    DataPointSeriesArchive,
    EventRecord,
    HealthScore,
    SeriesTypeDefinition,
    User,
    XmlUpload,
)
from app.models.wearable_history_source import WearableHistorySource
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import ProviderName
from app.schemas.enums.aggregation_method import AggregationMethod
from app.schemas.providers.mobile_sdk.sleep_state import SleepState
from app.services.sdk.sleep_service import load_sleep_state, save_sleep_state
from app.services.wearable_history_purge_service import WearableHistoryPurgeService
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    HealthScoreFactory,
    SeriesTypeDefinitionFactory,
    UserFactory,
)

START = date(2026, 9, 29)


def at(day: int, hour: int, minute: int = 0, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def make_source(db: Session, member: UUID, provider: str, *, frozen: bool) -> None:
    """Connected 1 Oct with nights back to 29 Sep; frozen since 8 Oct, or still provisional."""
    WearableHistoryRepository().begin_source(db, member, provider, at(1, 8, month=10))
    db.execute(
        text(
            "UPDATE wearable_history_sources SET start_date=:start, night_dates=:nights, freeze_at=:freeze "
            "WHERE user_id=:member AND provider=:provider"
        ),
        {
            "start": START,
            "nights": [date(2026, 9, 20), START, date(2026, 9, 30)],
            "freeze": at(7, 22, month=10) if frozen else datetime.now(UTC) + timedelta(days=3),
            "member": member,
            "provider": provider,
        },
    )


class Seeded:
    """One persisted row of each D-01 kind for one data source, pre-start and admitted."""

    def __init__(self, db: Session, member: User, provider: ProviderName) -> None:
        heart_rate = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        weight = SeriesTypeDefinitionFactory.get_or_create_weight()
        source = DataSourceFactory(user=member, provider=provider)
        self.source = source

        def series(when: datetime, kind: SeriesTypeDefinition = heart_rate) -> UUID:
            return DataPointSeriesFactory(data_source=source, series_type=kind, recorded_at=when).id

        def event(category: str, start: datetime, end: datetime) -> UUID:
            return EventRecordFactory(
                data_source=source,
                category=category,
                type="sleep_session" if category == "sleep" else "running",
                start_datetime=start,
                end_datetime=end,
                zone_offset="+02:00",
            ).id

        # Night of 28 Sep (pre-start): 27 Sep 22:00 to 28 Sep 06:00 Berlin.
        self.prestart_night = event("sleep", at(27, 20), at(28, 4))
        # A pre-start night running past midnight into the start date (05:00 on 28 Sep to
        # 01:00 on 29 Sep Berlin): its sample at 00:30 on 29 Sep has an admitted calendar
        # date, and is rejected only because the night is read before it is deleted.
        self.long_prestart_night = event("sleep", at(28, 3), at(28, 23))
        # Night of 29 Sep (the start date): its evening falls on 28 Sep.
        self.first_night = event("sleep", at(28, 23, 30), at(29, 5))
        self.prestart_workout = event("workout", at(27, 8), at(27, 9))
        self.admitted_workout = event("workout", at(2, 8, month=10), at(2, 9, month=10))

        self.prestart_day_sample = series(at(27, 12))
        self.prestart_night_sample = series(at(27, 23))
        self.long_night_sample = series(at(28, 22, 30))
        self.first_night_evening_sample = series(at(28, 23, 45))
        self.admitted_sample = series(at(29, 12))
        self.prestart_weight = series(at(20, 7), weight)

        def score(when: datetime, **kwargs) -> UUID:
            return HealthScoreFactory(provider=provider, recorded_at=when, **kwargs).id

        self.prestart_score = score(at(27, 12), data_source=source)
        self.admitted_score = score(at(30, 12), data_source=source)
        self.unattached_prestart_score = score(at(26, 12), data_source_id=None, user_id=member.id)
        self.workout_score = score(at(27, 9), data_source=source, event_record_id=self.prestart_workout)
        self.internal_score = HealthScoreFactory(
            # Internal scores are per member, so each seeded source gives its own a distinct time.
            provider=ProviderName.INTERNAL,
            recorded_at=at(26, 12, minute=1 if provider == ProviderName.APPLE else 2),
            data_source_id=None,
            user_id=member.id,
        ).id

        def archive(day: datetime, proved: date | None) -> UUID:
            row = DataPointSeriesArchive(
                id=uuid4(),
                data_source_id=source.id,
                series_type_definition_id=heart_rate.id,
                bucket_start_at=day,
                aggregation_type=AggregationMethod.AVG,
                value=Decimal(60),
                sample_count=10,
                history_start_date=proved,
            )
            db.add(row)
            return row.id

        self.prestart_archive = archive(at(25, 0), None)
        self.admitted_archive = archive(at(30, 0), None)
        db.flush()

    def deleted(self) -> set[UUID]:
        return {
            self.prestart_night,
            self.long_prestart_night,
            self.prestart_workout,
            self.prestart_day_sample,
            self.prestart_night_sample,
            self.long_night_sample,
            self.prestart_score,
            self.unattached_prestart_score,
            self.workout_score,
            self.prestart_archive,
        }

    def everything(self) -> set[UUID]:
        return self.deleted() | {
            self.first_night,
            self.admitted_workout,
            self.first_night_evening_sample,
            self.admitted_sample,
            self.prestart_weight,
            self.admitted_score,
            self.internal_score,
            self.admitted_archive,
        }


def present(db: Session, ids: set[UUID]) -> set[UUID]:
    found: set[UUID] = set()
    for model in (DataPointSeries, EventRecord, HealthScore, DataPointSeriesArchive):
        found |= set(db.scalars(select(model.id).where(model.id.in_(ids))))
    return found


def history(db: Session, member: UUID, provider: str) -> WearableHistorySource:
    db.expire_all()
    source = WearableHistoryRepository().get(db, member, provider)
    assert source is not None
    return source


def freeze(db: Session, member: UUID, provider: str) -> None:
    db.execute(
        text("UPDATE wearable_history_sources SET freeze_at=:freeze WHERE user_id=:member AND provider=:provider"),
        {"freeze": at(7, 22, month=10), "member": member, "provider": provider},
    )


def seed(db: Session) -> tuple[User, Seeded, Seeded, Seeded]:
    """Rows predate the sources, as a member's history did when 2.96.9.1 shipped; ingress and
    the archive trigger check only rows written after a source exists."""
    member = UserFactory()
    apple = Seeded(db, member, ProviderName.APPLE)
    garmin = Seeded(db, member, ProviderName.GARMIN)
    other = UserFactory()
    stranger = Seeded(db, other, ProviderName.APPLE)
    make_source(db, member.id, "apple", frozen=False)
    make_source(db, member.id, "garmin", frozen=False)
    make_source(db, other.id, "apple", frozen=False)
    freeze(db, member.id, "apple")
    db.commit()
    return member, apple, garmin, stranger


def test_purge_deletes_only_the_frozen_sources_prestart_rows(db: Session) -> None:
    member, apple, garmin, stranger = seed(db)

    run = WearableHistoryPurgeService().run(db)

    assert (run.due, run.completed, run.failed, run.waiting) == (1, 1, 0, 0)
    assert present(db, apple.everything()) == apple.everything() - apple.deleted()
    assert present(db, garmin.everything()) == garmin.everything()
    assert present(db, stranger.everything()) == stranger.everything()
    source = history(db, member.id, "apple")
    assert source.night_dates == [START, date(2026, 9, 30)]
    assert source.records_purged_at is not None
    assert source.purge_completed_at is not None
    assert (source.purge_attempts, source.purge_last_error) == (1, None)
    assert history(db, member.id, "garmin").records_purged_at is None
    assert WearableHistoryPurgeService().run(db).due == 0


def sleep_state(night_start: datetime) -> SleepState:
    return SleepState(
        uuid=str(uuid4()),
        provider="apple",
        zone_offset="+02:00",
        start_time=night_start,
        end_time=night_start + timedelta(hours=7),
        last_start_timestamp=night_start,
        last_end_timestamp=night_start + timedelta(hours=7),
    )


def test_apple_sleep_state_is_cleared_only_when_its_night_is_prestart(db: Session) -> None:
    member, *_ = seed(db)
    later = UserFactory()
    make_source(db, later.id, "apple", frozen=True)
    db.commit()
    save_sleep_state(str(member.id), sleep_state(at(27, 20)))  # night of 28 Sep: pre-start
    save_sleep_state(str(later.id), sleep_state(at(28, 20)))  # night of 29 Sep: the start date

    run = WearableHistoryPurgeService().run(db)

    assert run.completed == 2
    assert load_sleep_state(str(member.id)) is None
    assert load_sleep_state(str(later.id)) is not None


@pytest.mark.parametrize("state", ["pending_import", "imported", "failed"])
def test_apple_source_waits_for_upload_files_without_recording_an_error(db: Session, state: str) -> None:
    member, apple, *_ = seed(db)
    db.add(XmlUpload(id=uuid4(), user_id=member.id, bucket="b", object_key="k", state=state))
    db.commit()

    run = WearableHistoryPurgeService().run(db)

    assert (run.completed, run.failed, run.waiting) == (0, 0, 1)
    source = history(db, member.id, "apple")
    assert source.records_purged_at is not None
    assert present(db, apple.deleted()) == set()
    assert (source.purge_completed_at, source.purge_attempts, source.purge_last_error) == (None, 1, None)

    db.execute(text("UPDATE xml_uploads SET state='deleted' WHERE user_id=:m"), {"m": member.id})
    db.commit()
    assert WearableHistoryPurgeService().run(db).completed == 1
    assert history(db, member.id, "apple").purge_attempts == 2


def test_failed_records_transaction_rolls_back_whole_and_is_retried(db: Session) -> None:
    member, apple, *_ = seed(db)
    service = WearableHistoryPurgeService()
    original = service.purge_records

    def fail_after_deleting(db: Session, source: WearableHistorySource) -> dict[str, int]:
        original(db, source)
        raise RuntimeError("connection lost")

    with patch.object(service, "purge_records", side_effect=fail_after_deleting):
        run = service.run(db)

    assert (run.completed, run.failed) == (0, 1)
    source = history(db, member.id, "apple")
    assert (source.records_purged_at, source.purge_completed_at) == (None, None)
    assert (source.purge_attempts, source.purge_last_error) == (1, "records: RuntimeError")
    assert present(db, apple.deleted()) == apple.deleted()

    assert service.run(db).completed == 1
    assert present(db, apple.deleted()) == set()
    assert history(db, member.id, "apple").purge_attempts == 2


def test_failure_after_the_records_commit_keeps_the_purge_and_retries_the_rest(db: Session) -> None:
    member, apple, *_ = seed(db)
    service = WearableHistoryPurgeService()

    with patch.object(service, "clear_sleep_state", side_effect=ConnectionError("redis down")):
        run = service.run(db)

    assert (run.completed, run.failed) == (0, 1)
    source = history(db, member.id, "apple")
    assert source.records_purged_at is not None
    assert source.purge_completed_at is None
    assert (source.purge_attempts, source.purge_last_error) == (1, "sleep_state: ConnectionError")
    assert present(db, apple.deleted()) == set()

    assert service.run(db).completed == 1
    source = history(db, member.id, "apple")
    assert (source.purge_attempts, source.purge_last_error) == (2, None)


def test_the_purge_is_scheduled_every_fifteen_minutes_and_purges_due_sources(db: Session) -> None:
    entry = create_celery().conf.beat_schedule["purge-frozen-sources"]
    assert entry["task"] == purge_frozen_sources_task.name
    assert entry["schedule"].minute == set(range(0, 60, 15))
    member, apple, *_ = seed(db)
    with patch("app.integrations.celery.tasks.wearable_history_purge_task.SessionLocal", return_value=nullcontext(db)):
        assert purge_frozen_sources_task.run() == {"due": 1, "completed": 1, "failed": 0, "waiting": 0}
    assert present(db, apple.deleted()) == set()
    assert history(db, member.id, "apple").purge_completed_at is not None


def test_records_are_stamped_only_after_their_deletion_commits(db: Session) -> None:
    """D-03: an export requested after the stamp can only read the purged state, so the stamp
    is written by its own statement after the D-01 commit, never inside that transaction."""
    member, apple, *_ = seed(db)
    service = WearableHistoryPurgeService()
    order: list[str] = []
    real_commit, real_purge, real_stamp = db.commit, service.purge_records, service.stamp_records_purged

    def commit() -> None:
        order.append("commit")
        real_commit()

    def purge(db_: Session, source: WearableHistorySource) -> dict[str, int]:
        order.append("delete")
        return real_purge(db_, source)

    def stamp(db_: Session, source_id: UUID) -> None:
        order.append("stamp")
        real_stamp(db_, source_id)

    with (
        patch.object(db, "commit", side_effect=commit),
        patch.object(service, "purge_records", side_effect=purge),
        patch.object(service, "stamp_records_purged", side_effect=stamp),
    ):
        service.purge_source(db, member.id, "apple")
    assert order[:4] == ["delete", "commit", "stamp", "commit"]


def test_a_failed_stamp_leaves_the_records_purged_and_is_stamped_next_run(db: Session) -> None:
    member, apple, *_ = seed(db)
    service = WearableHistoryPurgeService()
    with patch.object(service, "stamp_records_purged", side_effect=ConnectionError("lost")):
        assert service.run(db).failed == 1
    source = history(db, member.id, "apple")
    assert present(db, apple.deleted()) == set()
    assert (source.records_purged_at, source.purge_last_error) == (None, "records: ConnectionError")
    assert service.run(db).completed == 1
    assert history(db, member.id, "apple").records_purged_at is not None


def test_a_failed_completion_is_recorded_and_the_other_due_sources_still_run(db: Session) -> None:
    """D-03: within a run, one failed source never stops the others, the last step included."""
    member, *_ = seed(db)
    freeze(db, member.id, "garmin")
    db.commit()
    service = WearableHistoryPurgeService()
    real = service.repository.mark_completed
    calls: list[str] = []

    def complete(db_: Session, member_: UUID, provider: str) -> None:
        calls.append(provider)
        if len(calls) == 1:
            raise ConnectionError("lost")
        real(db_, member_, provider)

    with patch.object(service.repository, "mark_completed", side_effect=complete):
        run = service.run(db)

    assert (run.due, run.completed, run.failed) == (2, 1, 1)
    failed = history(db, member.id, calls[0])
    assert (failed.purge_completed_at, failed.purge_last_error) == (None, "complete: ConnectionError")
    assert failed.records_purged_at is not None
    assert history(db, member.id, calls[1]).purge_completed_at is not None
