"""2.96.9.2 D-04: after the Apple freeze an XML upload skips every pre-start record."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.integrations.celery.tasks import process_aws_upload_task, process_xml_upload_task
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import SeriesType
from app.schemas.model_crud.activities import EventRecordCreate, TimeSeriesSampleCreate
from app.schemas.providers.mobile_sdk import SleepRecord, SyncRequest, SyncRequestData
from app.services.apple.apple_xml.wearable_history_filter import PrestartSkipper
from app.utils.wearable_history_ingress import PreStartDataRejectedError
from tests.factories import UserFactory

START = date(2026, 10, 1)


def frozen_member(db: Session, *, frozen: bool = True) -> str:
    member = UserFactory()
    # Connected 1 Oct: frozen since 8 Oct 00:00 Berlin. A future connection stays provisional.
    connected = datetime(2026, 10, 1, 8, tzinfo=UTC) if frozen else datetime.now(UTC) + timedelta(days=2)
    WearableHistoryRepository().begin_source(db, member.id, "apple", connected)
    db.commit()
    return str(member.id)


def sample(user_id: str, at: datetime, series: SeriesType = SeriesType.heart_rate) -> TimeSeriesSampleCreate:
    return TimeSeriesSampleCreate(
        id=uuid4(), user_id=user_id, source="apple_health_xml", recorded_at=at, value=Decimal(60), series_type=series
    )


def workout(user_id: str, at: datetime) -> tuple[EventRecordCreate, MagicMock]:
    record = EventRecordCreate(
        id=uuid4(),
        user_id=user_id,
        source="apple_health_xml",
        source_name="Apple Health",
        category="workout",
        type="running",
        start_datetime=at,
        end_datetime=at + timedelta(hours=1),
    )
    return record, MagicMock()


def sleep(start: datetime, hours: int = 8) -> SleepRecord:
    return SleepRecord(stage="asleep", startDate=start, endDate=start + timedelta(hours=hours), zoneOffset="+02:00")


def chunk(user_id: str) -> tuple[list, list, SyncRequest]:
    samples = [
        sample(user_id, datetime(2026, 9, 29, 12, tzinfo=UTC)),  # pre-start
        sample(user_id, datetime(2026, 9, 30, 20, tzinfo=UTC)),  # first night's evening: skipped too
        sample(user_id, datetime(2026, 10, 1, 12, tzinfo=UTC)),  # start date: kept
        sample(user_id, datetime(2026, 9, 29, 12, tzinfo=UTC), SeriesType.weight),  # weight: kept (R3)
    ]
    workouts = [
        workout(user_id, datetime(2026, 9, 30, 10, tzinfo=UTC)),
        workout(user_id, datetime(2026, 10, 2, 10, tzinfo=UTC)),
    ]
    nights = [sleep(datetime(2026, 9, 28, 20, tzinfo=UTC)), sleep(datetime(2026, 9, 30, 20, tzinfo=UTC))]
    request = SyncRequest(
        provider="apple_health_xml",
        sdkVersion="n/a",
        syncTimestamp=datetime.now(UTC),
        data=SyncRequestData(sleep=nights, records=[], workouts=[]),
    )
    return samples, workouts, request


def test_frozen_skipper_keeps_only_start_date_records_and_weight(db: Session) -> None:
    user_id = frozen_member(db)
    skipper = PrestartSkipper.for_member(db, user_id)
    assert skipper.start_date == START
    samples, workouts, request = chunk(user_id)
    kept_samples = skipper.samples(samples)
    assert [(s.recorded_at.date(), s.series_type) for s in kept_samples] == [
        (date(2026, 10, 1), SeriesType.heart_rate),
        (date(2026, 9, 29), SeriesType.weight),
    ]
    assert [r.start_datetime.date() for r, _ in skipper.workouts(workouts)] == [date(2026, 10, 2)]
    # The 30 Sep 22:00 (Berlin) night is the 1 Oct night: kept; the 28 Sep night is not.
    assert [r.startDate for r in skipper.sleep(request).data.sleep] == [datetime(2026, 9, 30, 20, tzinfo=UTC)]
    assert skipper.skipped == 4


def test_provisional_source_skips_nothing(db: Session) -> None:
    user_id = frozen_member(db, frozen=False)
    skipper = PrestartSkipper.for_member(db, user_id)
    samples, workouts, request = chunk(user_id)
    assert skipper.samples(samples) == samples
    assert skipper.workouts(workouts) == workouts
    assert skipper.sleep(request).data.sleep == request.data.sleep
    assert skipper.skipped == 0


@pytest.mark.parametrize("path", ["aws", "direct"])
@pytest.mark.parametrize("sleep_rejected", [False, True])
def test_both_xml_paths_write_only_admitted_records_and_complete(db: Session, path: str, sleep_rejected: bool) -> None:
    user_id = frozen_member(db)
    module = (
        "app.integrations.celery.tasks.process_aws_upload_task"
        if path == "aws"
        else "app.integrations.celery.tasks.process_xml_upload_task"
    )
    service = MagicMock()
    service.parse_xml.return_value = iter([chunk(user_id)])
    service.stats.skipped_prestart = 0
    written_samples: list = []
    with (
        patch(f"{module}.XMLService", return_value=service),
        patch(f"{module}.event_record_service") as events,
        patch(f"{module}.timeseries_service") as series,
        patch(
            f"{module}.handle_sleep_data",
            side_effect=PreStartDataRejectedError("state") if sleep_rejected else None,
        ) as sleep_handler,
    ):
        series.bulk_create_samples.side_effect = lambda _db, samples: written_samples.extend(samples)
        if path == "aws":
            skipped = process_aws_upload_task._import_xml_data(db, MagicMock(), user_id)
        else:
            skipped = process_xml_upload_task._import_xml_data(db, "/tmp/unused.xml", user_id).skipped_prestart
    assert [s.recorded_at.date() for s in written_samples] == [date(2026, 10, 1), date(2026, 9, 29)]
    assert [c.args[1].start_datetime.date() for c in events.create.call_args_list] == [date(2026, 10, 2)]
    assert [r.startDate for r in sleep_handler.call_args.args[1].data.sleep] == [datetime(2026, 9, 30, 20, tzinfo=UTC)]
    assert skipped == 4 + (1 if sleep_rejected else 0)


@pytest.mark.parametrize("path", ["aws", "direct"])
def test_an_import_crossing_the_freeze_switches_at_its_next_chunk(db: Session, path: str) -> None:
    user_id = frozen_member(db, frozen=False)
    module = (
        "app.integrations.celery.tasks.process_aws_upload_task"
        if path == "aws"
        else "app.integrations.celery.tasks.process_xml_upload_task"
    )

    def chunks():  # noqa: ANN202
        yield chunk(user_id)
        db.execute(
            text("UPDATE wearable_history_sources SET freeze_at=:f, start_date=:s WHERE user_id=:m"),
            {"f": datetime(2026, 10, 8, tzinfo=UTC), "s": START, "m": user_id},
        )
        yield chunk(user_id)

    service = MagicMock()
    service.parse_xml.return_value = chunks()
    service.stats.skipped_prestart = 0
    written: list = []
    with (
        patch(f"{module}.XMLService", return_value=service),
        patch(f"{module}.event_record_service"),
        patch(f"{module}.timeseries_service") as series,
        patch(f"{module}.handle_sleep_data"),
    ):
        series.bulk_create_samples.side_effect = lambda _db, samples: written.append(len(samples))
        if path == "aws":
            skipped = process_aws_upload_task._import_xml_data(db, MagicMock(), user_id)
        else:
            skipped = process_xml_upload_task._import_xml_data(db, "/tmp/unused.xml", user_id).skipped_prestart
    assert written == [4, 2]
    assert skipped == 4
