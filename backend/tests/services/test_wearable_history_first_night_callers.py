"""Actual Oura/Garmin caller persistence and retry boundaries."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.webhook_push_task import process_webhook_push
from app.models import DataPointSeries, DataSource, EventRecord, HealthScore, User
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.model_crud.activities import EventRecordCreate
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.webhook_handler import GarminWebhookHandler
from app.services.providers.oura.data_247 import Oura247Data
from app.utils.wearable_history_ingress import (
    FirstNightAssociationUnavailableError,
    WearableHistoryMetadataUnavailableError,
)
from tests.factories import DataSourceFactory, EventRecordFactory, UserConnectionFactory, UserFactory


def connected(db: Session, provider: str) -> User:
    member = UserFactory()
    WearableHistoryRepository().begin_source(db, member.id, provider, datetime(2026, 10, 13, 12, tzinfo=UTC))
    UserConnectionFactory(user=member, provider=provider, provider_user_id="provider-member")
    db.commit()
    return member


@pytest.mark.parametrize("metric", ["heart_rate", "hrv", "average_breath", "lowest_heart_rate"])
def test_oura_rejected_sleep_never_writes_its_companion_readings(db: Session, metric: str) -> None:
    member = connected(db, "oura")
    service = Oura247Data("oura", "https://provider.test", MagicMock())
    local = timezone(timedelta(hours=-10))
    day = datetime(2026, 10, 13, tzinfo=local)
    raws = []
    for index, start in enumerate([day - timedelta(hours=20), day - timedelta(hours=4), day + timedelta(hours=20)]):
        raw = {
            "id": str(uuid4()),
            "type": "long_sleep",
            "bedtime_start": start.isoformat(),
            "bedtime_end": (start + timedelta(hours=8)).isoformat(),
            "light_sleep_duration": 3600,
        }
        if metric in ("heart_rate", "hrv"):
            raw[metric] = {
                "timestamp": (start + timedelta(hours=7, minutes=55)).isoformat(),
                "interval": 300,
                "items": [None, 61 + index],
            }
        else:
            raw[metric] = 61 + index
        raws.append(raw)
    assert service.save_sleep_data(db, member.id, service.normalize_sleeps(raws, member.id)) == 2
    db.commit()
    source_ids = select(DataSource.id).where(DataSource.user_id == member.id)
    records = list(db.scalars(select(EventRecord).where(EventRecord.data_source_id.in_(source_ids))))
    assert len(records) == 2
    assert [
        row[0]
        for row in db.execute(
            select(DataPointSeries.value)
            .where(DataPointSeries.data_source_id.in_(source_ids))
            .order_by(DataPointSeries.value)
        )
    ] == [62, 63]


def garmin_sleep(start: datetime, score: int | None) -> dict:
    return {
        "summaryId": str(uuid4()),
        "startTimeInSeconds": int(start.timestamp()),
        "startTimeOffsetInSeconds": 0,
        "durationInSeconds": 28800,
        "lightSleepDurationInSeconds": 3600,
        "overallSleepScore": {"value": score},
    }


@pytest.mark.parametrize("replay", [False, True])
def test_garmin_first_night_score_uses_the_actual_inserted_or_deduplicated_parent(db: Session, replay: bool) -> None:
    member = connected(db, "garmin")
    service = Garmin247Data("garmin", "https://provider.test", MagicMock())
    first = datetime(2026, 10, 12, 20, tzinfo=UTC)
    raws = [
        garmin_sleep(first - timedelta(days=1), 61),
        garmin_sleep(first, 62),
        garmin_sleep(first + timedelta(days=1), 63),
    ]
    if replay:
        assert service.process_items_batch(db, member.id, "sleeps", raws[1:]) == 2
        db.query(HealthScore).filter(HealthScore.user_id == member.id, HealthScore.provider == "garmin").delete()
        db.commit()  # A prior partial attempt wrote the nights; the score must recover on replay.
    count = service.process_items_batch(db, member.id, "sleeps", raws)
    assert count == (0 if replay else 2)
    db.commit()
    source_ids = select(DataSource.id).where(DataSource.user_id == member.id)
    records = list(db.scalars(select(EventRecord).where(EventRecord.data_source_id.in_(source_ids))))
    scores = list(
        db.scalars(select(HealthScore).where(HealthScore.user_id == member.id, HealthScore.provider == "garmin"))
    )
    assert {r.start_datetime for r in records} == {first, first + timedelta(days=1)}
    assert {float(s.value) for s in scores} == {62, 63}
    assert {s.event_record_id for s in scores} == {r.id for r in records}


@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("scored", [False, True])
def test_garmin_unresolved_sample_retries_the_actual_task_and_recovers_after_its_night(
    db: Session, combined: bool, scored: bool
) -> None:
    member = connected(db, "garmin")
    service = Garmin247Data("garmin", "https://provider.test", MagicMock())
    handler = GarminWebhookHandler(MagicMock(), service)
    first = datetime(2026, 10, 12, 20, tzinfo=UTC)
    sleep = garmin_sleep(first, 62 if scored else None)
    sleep["userId"] = "provider-member"
    payload = {
        "epochs": [
            {
                "userId": "provider-member",
                "startTimeInSeconds": int(first.timestamp()),
                "durationInSeconds": 900,
                "meanHeartRateInBeatsPerMinute": 61,
            }
        ]
    }
    probe = service._build_epochs_samples(member.id, service.normalize_epochs(payload["epochs"], member.id))
    assert len(probe) == 1
    with pytest.raises(FirstNightAssociationUnavailableError):
        WearableHistoryIngressRepository(db).sample_admitted(probe[0])
    if combined:
        payload["sleeps"] = [sleep]

    @contextmanager
    def session() -> Iterator[Session]:
        try:
            yield db
        finally:
            db.rollback()  # Actual task-session closure discards uncommitted siblings.

    with (
        patch("app.integrations.celery.tasks.webhook_push_task.SessionLocal", session),
        patch(
            "app.integrations.celery.tasks.webhook_push_task.ProviderFactory",
            return_value=SimpleNamespace(get_provider=lambda _: SimpleNamespace(webhooks=handler)),
        ),
        patch("app.integrations.celery.tasks.webhook_push_task._emit_webhook_sync_status"),
        patch("app.services.providers.garmin.handlers.wellness.emit_sync_completed"),
        patch("app.services.providers.garmin.webhook_handler.mark_type_success", return_value=False),
        patch.object(
            process_webhook_push, "retry", side_effect=FirstNightAssociationUnavailableError("task retry")
        ) as retry,
    ):
        with pytest.raises(FirstNightAssociationUnavailableError, match="task retry"):
            process_webhook_push.run("garmin", payload, "first-night-test")
        assert retry.call_count == 1
        assert isinstance(retry.call_args.kwargs["exc"], FirstNightAssociationUnavailableError)
        source_ids = select(DataSource.id).where(DataSource.user_id == member.id)
        assert list(db.scalars(select(DataPointSeries).where(DataPointSeries.data_source_id.in_(source_ids)))) == []
        if not combined:
            assert service.process_items_batch(db, member.id, "sleeps", [sleep]) == 1
        result = process_webhook_push.run("garmin", payload, "first-night-redelivery")
        assert result["wellness"]["epochs"]["saved"] == 1
        assert retry.call_count == 1
    db.commit()
    assert [
        row[0]
        for row in db.execute(select(DataPointSeries.value).where(DataPointSeries.data_source_id.in_(source_ids)))
    ] == [61]


@pytest.mark.parametrize("field", ["member", "provider", "writer", "device", "category", "start", "end"])
def test_garmin_parent_resolution_does_not_cross_a_persisted_namespace(db: Session, field: str) -> None:
    member = connected(db, "garmin")
    first = datetime(2026, 10, 13, 20, tzinfo=UTC)
    sibling_member = UserFactory() if field == "member" else member
    source = DataSourceFactory(
        user=sibling_member,
        provider="oura" if field == "provider" else "garmin",
        source="alternate" if field == "writer" else "garmin",
        device_model="alternate" if field == "device" else None,
    )
    sibling = EventRecordFactory(
        data_source=source,
        category="workout" if field == "category" else "sleep",
        start_datetime=first + (timedelta(minutes=1) if field == "start" else timedelta()),
        end_datetime=first + timedelta(hours=8) + (timedelta(minutes=1) if field == "end" else timedelta()),
        zone_offset="+00:00",
    )
    db.commit()
    creator = EventRecordCreate(
        id=uuid4(),
        user_id=member.id,
        source="garmin",
        source_name="Garmin",
        category="sleep",
        type="sleep_session",
        start_datetime=first,
        end_datetime=first + timedelta(hours=8),
    )
    repo = EventRecordRepository(EventRecord)
    assert repo.find_persisted_for_creator(db, creator) is None
    exact = creator.model_copy(
        update={
            "user_id": sibling_member.id,
            "provider": source.provider,
            "source": source.source,
            "device_model": source.device_model,
            "category": sibling.category,
            "start_datetime": sibling.start_datetime,
            "end_datetime": sibling.end_datetime,
        }
    )
    assert repo.find_persisted_for_creator(db, exact).id == sibling.id


@pytest.mark.parametrize(
    "failure", [FirstNightAssociationUnavailableError, WearableHistoryMetadataUnavailableError, RuntimeError]
)
def test_garmin_task_retries_history_failures_but_preserves_ordinary_error_handling(
    db: Session, failure: type[RuntimeError]
) -> None:
    connected(db, "garmin")
    service = Garmin247Data("garmin", "https://provider.test", MagicMock())
    handler = GarminWebhookHandler(MagicMock(), service)

    @contextmanager
    def session() -> Iterator[Session]:
        yield db

    with (
        patch("app.integrations.celery.tasks.webhook_push_task.SessionLocal", session),
        patch(
            "app.integrations.celery.tasks.webhook_push_task.ProviderFactory",
            return_value=SimpleNamespace(get_provider=lambda _: SimpleNamespace(webhooks=handler)),
        ),
        patch.object(service, "process_items_batch", side_effect=failure("history unavailable")),
        patch.object(process_webhook_push, "retry", side_effect=failure("task retry")) as retry,
    ):
        payload = {"epochs": [{"userId": "provider-member"}]}
        if failure is RuntimeError:
            result = process_webhook_push.run("garmin", payload, "ordinary-error")
            assert result["errors"] == ["epochs error: history unavailable"]
            retry.assert_not_called()
        else:
            with pytest.raises(failure, match="task retry"):
                process_webhook_push.run("garmin", payload, "retry-error")
            retry.assert_called_once()
            assert isinstance(retry.call_args.kwargs["exc"], failure)


def test_oura_failed_sleep_never_writes_orphaned_companions(db: Session) -> None:
    member = connected(db, "oura")
    service = Oura247Data("oura", "https://provider.test", MagicMock())
    first = datetime(2026, 10, 13, 20, tzinfo=UTC)
    raw = {
        "id": str(uuid4()),
        "type": "long_sleep",
        "bedtime_start": first.isoformat(),
        "bedtime_end": (first + timedelta(hours=8)).isoformat(),
        "light_sleep_duration": 3600,
        "heart_rate": {"timestamp": first.isoformat(), "interval": 300, "items": [61]},
    }
    with patch(
        "app.services.providers.oura.data_247.event_record_service.create_or_merge_sleep",
        side_effect=RuntimeError("ordinary failure"),
    ):
        assert service.save_sleep_data(db, member.id, service.normalize_sleeps([raw], member.id)) == 0
    source_ids = select(DataSource.id).where(DataSource.user_id == member.id)
    assert list(db.scalars(select(DataPointSeries).where(DataPointSeries.data_source_id.in_(source_ids)))) == []
