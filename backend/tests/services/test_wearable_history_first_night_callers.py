"""Actual Oura/Garmin caller persistence and retry boundaries."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.webhook_push_task import process_webhook_push
from app.models import DataPointSeries, DataSource, EventRecord, HealthScore, User, UserConnection
from app.models.wearable_history_source import WearableHistorySource
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
def test_garmin_unresolved_sample_is_dropped_without_a_task_retry_and_kept_after_its_night(
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
    assert WearableHistoryIngressRepository(db).sample_admitted(probe[0]) is False
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
        # A pre-start sample with no stored night is dropped; the delivery never retries.
        process_webhook_push.run("garmin", payload, "first-night-test")
        source_ids = select(DataSource.id).where(DataSource.user_id == member.id)
        if not combined:
            assert list(db.scalars(select(DataPointSeries).where(DataPointSeries.data_source_id.in_(source_ids)))) == []
            assert service.process_items_batch(db, member.id, "sleeps", [sleep]) == 1
        # Once its own night is stored, the same sample is kept.
        process_webhook_push.run("garmin", payload, "first-night-redelivery")
        assert retry.call_count == 0
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


@pytest.mark.parametrize("failure", [WearableHistoryMetadataUnavailableError])
@pytest.mark.parametrize("failed_first", [False, True])
def test_garmin_history_retry_preserves_other_members_of_the_same_provider_account(
    db: Session, failure: type[RuntimeError], failed_first: bool
) -> None:
    blocked = connected(db, "garmin")
    admitted = connected(db, "garmin")
    service = Garmin247Data("garmin", "https://provider.test", MagicMock())
    handler = GarminWebhookHandler(MagicMock(), service)
    first = datetime(2026, 10, 12, 20, tzinfo=UTC)
    assert service.process_items_batch(db, admitted.id, "sleeps", [garmin_sleep(first, None)]) == 1
    for member, position in [(blocked, 0 if failed_first else 1), (admitted, 1 if failed_first else 0)]:
        connection = db.scalar(select(UserConnection).where(UserConnection.user_id == member.id))
        connection.created_at = datetime(2026, 10, 13, 12, position, tzinfo=UTC)
    db.commit()
    original_source = WearableHistoryIngressRepository.source

    def source_read(self: WearableHistoryIngressRepository, identity: tuple) -> object:
        # A metadata outage is a failed read; a missing row of a connected source is repaired.
        if failure is WearableHistoryMetadataUnavailableError and identity[0] == blocked.id:
            raise WearableHistoryMetadataUnavailableError("Connected source history is unavailable")
        return original_source(self, identity)

    payload = {
        "epochs": [
            {
                "userId": "provider-member",
                "startTimeInSeconds": int(first.timestamp()),
                "startTimeOffsetInSeconds": 0,
                "meanHeartRateInBeatsPerMinute": 61,
            }
        ]
    }

    @contextmanager
    def session() -> Iterator[Session]:
        try:
            yield db
        finally:
            db.rollback()

    with (
        patch.object(WearableHistoryIngressRepository, "source", source_read),
        patch("app.integrations.celery.tasks.webhook_push_task.SessionLocal", session),
        patch(
            "app.integrations.celery.tasks.webhook_push_task.ProviderFactory",
            return_value=SimpleNamespace(get_provider=lambda _: SimpleNamespace(webhooks=handler)),
        ),
        patch("app.services.providers.garmin.handlers.wellness.emit_sync_completed"),
        patch("app.services.providers.garmin.webhook_handler.mark_type_success", return_value=True) as marked,
        patch.object(GarminWebhookHandler, "_chain_backfills", return_value=[]) as chained,
        patch.object(process_webhook_push, "retry", side_effect=failure("task retry")) as retry,
    ):
        for delivery in range(4):  # Initial delivery plus the unchanged three retries.
            with pytest.raises(failure, match="task retry"):
                process_webhook_push.run("garmin", payload, f"multi-member-{delivery}")
            assert retry.call_count == delivery + 1
            assert isinstance(retry.call_args.kwargs["exc"], failure)
            # The admitted member is marked and chained before the blocked one retries.
            assert marked.call_args_list[-1].args == (str(admitted.id), "epochs")
            assert chained.call_args.args[0] == {str(admitted.id)}
            source_ids = select(DataSource.id).where(DataSource.user_id == admitted.id)
            assert list(
                db.scalars(select(DataPointSeries.value).where(DataPointSeries.data_source_id.in_(source_ids)))
            ) == [61]
            blocked_sources = select(DataSource.id).where(DataSource.user_id == blocked.id)
            assert (
                list(db.scalars(select(DataPointSeries).where(DataPointSeries.data_source_id.in_(blocked_sources))))
                == []
            )


@pytest.mark.parametrize("blocked_first", [False, True])
def test_garmin_freeze_crossing_sql_rejection_retries_instead_of_dropping_the_batch(
    db: Session, blocked_first: bool
) -> None:
    blocked = connected(db, "garmin")
    admitted = connected(db, "garmin")
    # Both sources are frozen by the database clock; only the admitted one reaches 5 Oct.
    db.query(WearableHistorySource).filter(WearableHistorySource.user_id == admitted.id).update(
        {WearableHistorySource.start_date: datetime(2026, 10, 1).date()}
    )
    for member, position in [(blocked, 0 if blocked_first else 1), (admitted, 1 if blocked_first else 0)]:
        connection = db.scalar(select(UserConnection).where(UserConnection.user_id == member.id))
        connection.created_at = datetime(2026, 10, 13, 12, position, tzinfo=UTC)
    db.commit()
    sample_at = datetime(2026, 10, 5, 12, tzinfo=UTC)
    payload = {
        "epochs": [
            {
                "userId": "provider-member",
                "startTimeInSeconds": int(sample_at.timestamp()),
                "startTimeOffsetInSeconds": 0,
                "meanHeartRateInBeatsPerMinute": 61,
            }
        ]
    }
    original_source = WearableHistoryIngressRepository.source

    def stale_gate(self: WearableHistoryIngressRepository, identity: tuple) -> object:
        source = original_source(self, identity)
        if identity[0] == blocked.id:
            # The Python gate read its clock just before the freeze; SQL reads after it.
            self.now = source.freeze_at - timedelta(seconds=1)
        return source

    @contextmanager
    def session() -> Iterator[Session]:
        # The test transaction never commits, so check the deferred guard at write time,
        # where a real commit would check it.
        db.execute(text("SET CONSTRAINTS wh_ingress IMMEDIATE"))
        try:
            yield db
        finally:
            db.rollback()

    def stored(member: User) -> list:
        source_ids = select(DataSource.id).where(DataSource.user_id == member.id)
        return list(db.scalars(select(DataPointSeries.value).where(DataPointSeries.data_source_id.in_(source_ids))))

    handler = GarminWebhookHandler(MagicMock(), Garmin247Data("garmin", "https://provider.test", MagicMock()))
    common = (
        patch("app.integrations.celery.tasks.webhook_push_task.SessionLocal", session),
        patch(
            "app.integrations.celery.tasks.webhook_push_task.ProviderFactory",
            return_value=SimpleNamespace(get_provider=lambda _: SimpleNamespace(webhooks=handler)),
        ),
        patch("app.services.providers.garmin.handlers.wellness.emit_sync_completed"),
        patch.object(GarminWebhookHandler, "_chain_backfills", return_value=[]),
    )
    with (
        common[0],
        common[1],
        common[2],
        common[3],
        patch.object(WearableHistoryIngressRepository, "source", stale_gate),
        patch("app.services.providers.garmin.webhook_handler.mark_type_success", return_value=True) as marked,
        patch.object(
            process_webhook_push, "retry", side_effect=WearableHistoryMetadataUnavailableError("task retry")
        ) as retry,
        pytest.raises(WearableHistoryMetadataUnavailableError, match="task retry"),
    ):
        process_webhook_push.run("garmin", payload, "freeze-crossing")
    assert isinstance(retry.call_args.kwargs["exc"], WearableHistoryMetadataUnavailableError)
    # The rolled-back session is not reused: nothing after the blocked member runs or commits.
    assert marked.call_args_list == []
    assert stored(admitted) == ([] if blocked_first else [61])
    assert stored(blocked) == []

    # The redelivery's gate reads the frozen clock, rejects the pre-start sample and admits the rest.
    with (
        common[0],
        common[1],
        common[2],
        common[3],
        patch("app.services.providers.garmin.webhook_handler.mark_type_success", return_value=True) as marked,
    ):
        process_webhook_push.run("garmin", payload, "freeze-crossing-retry")
    # A permanent pre-start rejection is a finished delivery, so both members are marked.
    assert {c.args for c in marked.call_args_list} == {(str(admitted.id), "epochs"), (str(blocked.id), "epochs")}
    assert stored(admitted) == [61]
    assert stored(blocked) == []


def test_garmin_member_blocked_on_one_type_is_not_chained_by_another_and_lifecycle_waits() -> None:
    blocked, other = str(uuid4()), str(uuid4())
    results = {
        "epochs": {
            "processed": 2,
            "saved": 1,
            "succeeded_users": [other],
            "blocked_users": [blocked],
            "retry_error": FirstNightAssociationUnavailableError("first night"),
        },
        "sleeps": {"processed": 1, "saved": 1, "succeeded_users": [blocked], "blocked_users": [], "retry_error": None},
    }
    handler = GarminWebhookHandler(MagicMock(), MagicMock())
    with (
        patch(
            "app.services.providers.garmin.webhook_handler.process_wellness_items",
            side_effect=lambda _db, _repo, _svc, data_type, *_: results[data_type],
        ),
        patch("app.services.providers.garmin.webhook_handler.mark_type_success", return_value=True),
        patch.object(handler, "_chain_backfills", return_value=[]) as chained,
        patch.object(handler, "_process_lifecycle_events") as lifecycle,
        pytest.raises(FirstNightAssociationUnavailableError),
    ):
        handler.process_payload(MagicMock(), {"epochs": [{}], "sleeps": [{}], "deregistrations": [{}]}, "trace")
    chained.assert_called_once_with({other}, "trace")
    lifecycle.assert_not_called()
