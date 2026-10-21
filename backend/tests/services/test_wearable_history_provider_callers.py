"""Real provider pull/webhook regressions for permanent sleep rejection."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, EventRecord, HealthScore, SleepDetails
from app.schemas.providers.whoop import WhoopWebhookNotificationType
from app.services.providers.suunto.data_247 import Suunto247Data
from app.services.providers.suunto.webhook_handler import SuuntoWebhookHandler
from app.services.providers.whoop.data_247 import Whoop247Data
from app.services.providers.whoop.webhook_handler import WhoopWebhookHandler
from tests.services.test_wearable_history_ingress_closure import frozen_source


def sleep_payload(provider: str, start: datetime, end: datetime, value: int) -> dict[str, Any]:
    if provider == "suunto":
        return {
            "timestamp": start.isoformat(),
            "entryData": {
                "SleepId": str(uuid4()),
                "BedtimeStart": start.isoformat(),
                "BedtimeEnd": end.isoformat(),
                "Duration": int((end - start).total_seconds()),
                "LightSleepDuration": 3600,
                "HRMin": value,
            },
        }
    return {
        "id": str(uuid4()),
        "cycle_id": "cycle-to-refresh",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "zone_offset": "+00:00",
        "score_state": "SCORED",
        "score": {
            "stage_summary": {"total_light_sleep_time_milli": 3_600_000},
            "respiratory_rate": value,
            "sleep_performance_percentage": value,
        },
    }


@pytest.mark.parametrize("provider", ["suunto", "whoop"])
@pytest.mark.parametrize("path", ["direct", "pull", "webhook"])
def test_provider_sleep_rejection_excludes_saved_counts_and_dependent_writes(
    db: Session,
    provider: str,
    path: str,
) -> None:
    member, history = frozen_source(db, provider)
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    db.commit()
    service = (Suunto247Data if provider == "suunto" else Whoop247Data)(provider, "https://provider.test", MagicMock())
    # A held early-morning session can end on the cutoff day. Its companion
    # timestamp alone would pass the daily gate, so checking persisted series
    # demonstrates that the sleep-result consumer actually stops that write.
    raws = [
        sleep_payload(provider, day - timedelta(hours=21), day + timedelta(hours=3), 61),
        sleep_payload(provider, day - timedelta(hours=4), day + timedelta(hours=4), 62),
        sleep_payload(provider, day + timedelta(hours=20), day + timedelta(days=1, hours=4), 63),
    ]
    normalized_ids: list[UUID] = []
    if provider == "suunto":
        assert isinstance(service, Suunto247Data)
        if path == "direct":
            outcomes = []
            for raw in raws:
                normalized = service.normalize_sleep(raw, member.id)
                normalized_ids.append(normalized["id"])
                outcomes.append(service.save_sleep_data(db, member.id, normalized))
            assert outcomes == [False, True, True]
        elif path == "pull":
            with patch.object(service, "get_sleep_data", return_value=raws):
                assert service.load_and_save_sleep(db, member.id, day - timedelta(days=2), day + timedelta(days=2)) == (
                    2,
                    1,
                )
        else:
            handler = SuuntoWebhookHandler(MagicMock(), service)
            result = handler._process_sleep(db, member.id, {"samples": raws}, "caller-test")
            assert result["saved_count"] == 2
            assert result["total_samples"] == 3
    else:
        assert isinstance(service, Whoop247Data)
        normalized_ids = [UUID(raw["id"]) for raw in raws]
        if path == "direct":
            outcomes = [
                service.save_sleep_data(db, member.id, service.normalize_sleep(raw, member.id)[0]) for raw in raws
            ]
            assert [result is not None for result in outcomes] == [False, True, True]
        elif path == "pull":
            with patch.object(service, "_fetch_paginated", return_value=(raws, True)):
                assert service.load_and_save_sleep(db, member.id, day - timedelta(days=2), day + timedelta(days=2)) == (
                    2,
                    True,
                )
        else:
            handler = WhoopWebhookHandler(service, MagicMock())
            counts = []
            with patch.object(service, "load_single_cycle", return_value=0) as cycle:
                for raw in raws:
                    with patch.object(service, "get_sleep_record", return_value=raw):
                        result = handler._handle_updated(
                            db, WhoopWebhookNotificationType.SLEEP_UPDATED, member.id, raw["id"]
                        )
                    counts.append(result["records_saved"])
                assert cycle.call_count == 3  # Cycle refresh is an independent admission path.
            assert counts == [0, 1, 1]
    db.commit()
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    source_ids = select(DataSource.id).where(DataSource.user_id == member.id)
    events = list(db.scalars(select(EventRecord).where(EventRecord.data_source_id.in_(source_ids))))
    assert {event.start_datetime for event in events} == {day - timedelta(hours=4), day + timedelta(hours=20)}
    if normalized_ids:
        assert db.get(EventRecord, normalized_ids[0]) is None
        assert all(db.get(EventRecord, record_id) is not None for record_id in normalized_ids[1:])
    details = list(db.scalars(select(SleepDetails).where(SleepDetails.record_id.in_([event.id for event in events]))))
    assert len(details) == 2
    assert [
        row[0]
        for row in db.execute(
            select(DataPointSeries.value)
            .where(DataPointSeries.data_source_id.in_(source_ids))
            .order_by(DataPointSeries.value)
        )
    ] == [62, 63]
    if provider == "whoop" and path != "direct":
        assert [
            row[0]
            for row in db.execute(
                select(HealthScore.value)
                .where(HealthScore.user_id == member.id, HealthScore.provider == "whoop")
                .order_by(HealthScore.value)
            )
        ] == [62, 63]
        scores = list(
            db.scalars(select(HealthScore).where(HealthScore.user_id == member.id, HealthScore.provider == "whoop"))
        )
        assert {score.event_record_id for score in scores} == {record.id for record in events}


@pytest.mark.parametrize("path", ["pull", "webhook"])
def test_whoop_merged_sleep_scores_follow_the_actual_persisted_night(db: Session, path: str) -> None:
    member, history = frozen_source(db, "whoop")
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    db.commit()
    service = Whoop247Data("whoop", "https://provider.test", MagicMock())
    raws = [
        sleep_payload("whoop", day - timedelta(hours=4), day + timedelta(hours=1), 62),
        sleep_payload("whoop", day + timedelta(hours=1), day + timedelta(hours=4), 63),
    ]
    if path == "pull":
        with patch.object(service, "_fetch_paginated", return_value=(raws, False)):
            assert service.load_and_save_sleep(db, member.id, day - timedelta(days=1), day + timedelta(days=1)) == (
                2,
                False,
            )
    else:
        for raw in raws:
            with patch.object(service, "get_sleep_record", return_value=raw):
                assert service.load_single_sleep(db, member.id, raw["id"]) == (1, "cycle-to-refresh")
    db.commit()
    source_ids = select(DataSource.id).where(DataSource.user_id == member.id)
    records = list(db.scalars(select(EventRecord).where(EventRecord.data_source_id.in_(source_ids))))
    assert len(records) == 1
    assert records[0].start_datetime == day - timedelta(hours=4)
    assert records[0].end_datetime == day + timedelta(hours=4)
    scores = list(
        db.scalars(select(HealthScore).where(HealthScore.user_id == member.id, HealthScore.provider == "whoop"))
    )
    assert len(scores) == 1
    assert scores[0].value == 63
    assert scores[0].event_record_id == records[0].id


@pytest.mark.parametrize("provider", ["suunto", "whoop"])
@pytest.mark.parametrize("failure", ["missing-window", "write-failure"])
def test_unsaved_provider_sleep_never_reports_success_or_runs_companions(
    db: Session,
    provider: str,
    failure: str,
) -> None:
    member, history = frozen_source(db, provider)
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    service = (Suunto247Data if provider == "suunto" else Whoop247Data)(provider, "https://provider.test", MagicMock())
    raw = sleep_payload(provider, day - timedelta(hours=4), day + timedelta(hours=4), 62)
    normalized = (
        service.normalize_sleep(raw, member.id)[0]
        if isinstance(service, Whoop247Data)
        else service.normalize_sleep(raw, member.id)
    )
    if failure == "missing-window":
        normalized["start_time"] = None
    module = f"app.services.providers.{provider}.data_247"
    with (
        patch(
            f"{module}.event_record_service.create_or_merge_sleep", side_effect=RuntimeError("write failed")
        ) as writer,
        patch(f"{module}.timeseries_service.bulk_create_samples") as series,
    ):
        result = service.save_sleep_data(db, member.id, normalized)
    if provider == "suunto":
        assert result is False
    else:
        assert result is None
    assert writer.call_count == (failure == "write-failure")
    series.assert_not_called()


@pytest.mark.parametrize("path", ["pull", "webhook"])
def test_whoop_score_failure_keeps_the_night_unsaved_and_omits_companion_series(db: Session, path: str) -> None:
    member, history = frozen_source(db, "whoop")
    day = datetime.combine(history.start_date, datetime.min.time(), UTC)
    db.commit()
    service = Whoop247Data("whoop", "https://provider.test", MagicMock())
    raw = sleep_payload("whoop", day - timedelta(hours=4), day + timedelta(hours=4), 62)
    with patch(
        "app.services.providers.whoop.data_247.health_score_service.bulk_create",
        side_effect=RuntimeError("score failed"),
    ):
        if path == "pull":
            with patch.object(service, "_fetch_paginated", return_value=([raw], False)):
                assert service.load_and_save_sleep(db, member.id, day - timedelta(days=1), day + timedelta(days=1)) == (
                    0,
                    False,
                )
        else:
            with patch.object(service, "get_sleep_record", return_value=raw):
                assert service.load_single_sleep(db, member.id, raw["id"]) == (0, "cycle-to-refresh")
    db.commit()
    source_ids = select(DataSource.id).where(DataSource.user_id == member.id)
    assert list(db.scalars(select(EventRecord).where(EventRecord.data_source_id.in_(source_ids)))) == []
    assert list(db.scalars(select(DataPointSeries).where(DataPointSeries.data_source_id.in_(source_ids)))) == []
    assert list(db.scalars(select(HealthScore).where(HealthScore.user_id == member.id))) == []
