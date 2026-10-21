import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, User, WearableHistorySource
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from tests.factories import DataPointSeriesFactory, DataSourceFactory, EventRecordFactory, UserFactory


def frozen_source(db: Session, provider: str = "apple") -> tuple[User, WearableHistorySource]:
    member = UserFactory()
    source = WearableHistoryRepository().begin_source(db, member.id, provider)
    source.freeze_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    return member, source


@pytest.mark.parametrize("offload", [False, True])
@pytest.mark.parametrize("metric", ["STEP_COUNT", "HEART_RATE", "HEART_RATE_VARIABILITY"])
def test_frozen_sdk_transport_uses_persisted_start_timestamp_and_strips_sleep_values(
    db: Session, offload: bool, metric: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.api.routes.v1.sdk_sync import sync_sdk_data
    from app.config import settings
    from app.services import raw_payload_storage

    member, source = frozen_source(db)
    day = datetime.combine(source.start_date, datetime.min.time(), UTC)
    old = day - timedelta(hours=12)
    night_start = day - timedelta(hours=4)
    body = {
        "provider": "apple",
        "sdkVersion": "1",
        "syncTimestamp": datetime.now(UTC).isoformat(),
        "data": {
            "records": [
                {
                    "id": "held-spanning",
                    "type": metric,
                    "value": 42,
                    "unit": "count",
                    "startDate": old.isoformat(),
                    "endDate": (day + timedelta(hours=12)).isoformat(),
                },
                {
                    "id": "admitted",
                    "type": metric,
                    "value": 43,
                    "unit": "count",
                    "startDate": day.isoformat(),
                    "endDate": day.isoformat(),
                },
                {
                    "id": "exempt-weight",
                    "type": "WEIGHT",
                    "value": 70,
                    "unit": "kg",
                    "startDate": old.isoformat(),
                    "endDate": day.isoformat(),
                },
            ],
            "sleep": [
                {
                    "stage": "light",
                    "startDate": night_start.isoformat(),
                    "endDate": (day + timedelta(hours=4)).isoformat(),
                    "values": [{"timestamp": (old - timedelta(days=20)).isoformat(), "value": 99, "type": "heartRate"}],
                }
            ],
        },
    }

    @contextmanager
    def archive_session() -> Iterator[Session]:
        yield db

    s3 = MagicMock()
    with (
        patch("app.database.SessionLocal", archive_session),
        patch.object(settings, "sdk_payload_s3_offload", offload),
        patch.object(raw_payload_storage, "_storage_backend", "log"),
        patch.object(raw_payload_storage, "_s3_client", s3),
        patch.object(raw_payload_storage, "_s3_bucket", "test-bucket"),
        patch("app.api.routes.v1.sdk_sync.load_sleep_state", return_value=None),
        patch("app.api.routes.v1.sdk_sync.process_sdk_upload.delay") as queue,
    ):
        response = sync_sdk_data(str(member.id), body, SimpleNamespace(auth_type="api_key"), db)
    assert response.status_code == 202
    if offload:
        stored = json.loads(s3.put_object.call_args.kwargs["Body"])
        assert queue.call_args.kwargs["content"] is None
        assert queue.call_args.kwargs["payload_ref"].startswith("s3://test-bucket/")
    else:
        stored = json.loads(queue.call_args.kwargs["content"])
        archival = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"raw_payload"' in line]
        assert archival == []  # Optional opaque archive is disabled for a frozen source.
    assert [r["id"] for r in stored["data"]["records"]] == ["admitted", "exempt-weight"]
    assert len(stored["data"]["sleep"]) == 1
    assert stored["data"]["sleep"][0]["values"] is None


@pytest.mark.parametrize("provider", ["apple", "polar"])
@pytest.mark.parametrize(
    "metric",
    [
        "heart_rate",
        "heart_rate_variability_sdnn",
        "heart_rate_variability_rmssd",
        "skin_temperature",
        "resting_heart_rate",
        "respiratory_rate",
        "oxygen_saturation",
    ],
)
@pytest.mark.parametrize("path", ["bulk", "orm"])
def test_frozen_daytime_nap_samples_use_calendar_date_and_persist(
    db: Session, provider: str, metric: str, path: str
) -> None:
    member, source = frozen_source(db, provider)
    data_source = DataSourceFactory(user=member, provider=provider)
    day = datetime.combine(source.start_date, datetime.min.time(), UTC) + timedelta(hours=11)
    EventRecordFactory(
        data_source=data_source, category="sleep", start_datetime=day, end_datetime=day + timedelta(hours=1)
    )
    instant = day + timedelta(minutes=30)
    identity = uuid4()
    if path == "bulk":
        creator = TimeSeriesSampleCreate(
            id=identity,
            user_id=member.id,
            provider=provider,
            source=provider,
            recorded_at=instant,
            series_type=SeriesType(metric),
            value=61,
        )
        counts = DataPointSeriesRepository(DataPointSeries).bulk_create(db, [creator])
        assert counts.inserted == 1
    else:
        DataPointSeriesFactory(
            id=identity,
            data_source=data_source,
            recorded_at=instant,
            series_type_definition_id=get_series_type_id(SeriesType(metric)),
            value=61,
        )
    db.flush()
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    persisted = db.scalar(select(DataPointSeries).where(DataPointSeries.id == identity))
    assert persisted is not None
    assert persisted.recorded_at == instant
    assert float(persisted.value) == 61


def test_frozen_validation_salvage_reports_safe_diagnostics_before_transport(db: Session) -> None:
    from app.services.sdk.wearable_history import sanitize_sdk_payload

    member, source = frozen_source(db)
    instant = datetime.combine(source.start_date, datetime.min.time(), UTC) + timedelta(hours=12)
    raw = {
        "provider": "apple",
        "sdkVersion": "1",
        "syncTimestamp": datetime.now(UTC).isoformat(),
        "data": {
            "records": [
                {
                    "type": "HEART_RATE",
                    "value": "private-invalid-value",
                    "unit": "count",
                    "startDate": instant.isoformat(),
                    "endDate": instant.isoformat(),
                },
                {
                    "type": "STEP_COUNT",
                    "value": 42,
                    "unit": "count",
                    "startDate": instant.isoformat(),
                    "endDate": instant.isoformat(),
                },
            ]
        },
    }
    with patch("sentry_sdk.push_scope") as scope, patch("sentry_sdk.capture_message") as capture:
        sanitized = sanitize_sdk_payload(db, member.id, raw)
    assert len(sanitized["data"]["records"]) == 1
    capture.assert_called_once()
    scope.return_value.__enter__.return_value.set_level.assert_called_once_with("warning")
    context = scope.return_value.__enter__.return_value.set_context.call_args.args[1]
    assert context["dropped_count"] == 1
    assert context["errors"][0]["collection"] == "records"
    assert context["errors"][0]["loc"] == "value"
    assert "private-invalid-value" not in json.dumps(context)
    assert "private-invalid-value" not in json.dumps(sanitized)
    with patch("sentry_sdk.capture_message") as second:
        assert sanitize_sdk_payload(db, member.id, sanitized) == sanitized
    second.assert_not_called()


@pytest.mark.parametrize("metric", ["HEART_RATE", "HEART_RATE_VARIABILITY"])
def test_frozen_sdk_daytime_nap_samples_survive_worker_transport_and_persist(db: Session, metric: str) -> None:
    from app.services.sdk.import_service import import_service
    from app.services.sdk.wearable_history import sanitize_sdk_payload

    member, source = frozen_source(db)
    start = datetime.combine(source.start_date, datetime.min.time(), UTC) + timedelta(hours=11)
    instant = start + timedelta(minutes=30)
    raw = {
        "provider": "apple",
        "sdkVersion": "1",
        "syncTimestamp": datetime.now(UTC).isoformat(),
        "data": {
            "sleep": [
                {"stage": "light", "startDate": start.isoformat(), "endDate": (start + timedelta(hours=1)).isoformat()}
            ],
            "records": [
                {
                    "type": metric,
                    "value": 62,
                    "unit": "count",
                    "startDate": instant.isoformat(),
                    "endDate": instant.isoformat(),
                }
            ],
        },
    }
    sanitized = sanitize_sdk_payload(db, member.id, raw)
    assert len(sanitized["data"]["records"]) == 1
    with patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps.delay"):
        result = import_service.load_data(db, sanitized, str(member.id))
    assert result["records_inserted"] == 1
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    persisted = list(
        db.scalars(
            select(DataPointSeries)
            .join(DataSource)
            .where(DataSource.user_id == member.id, DataPointSeries.recorded_at == instant)
        )
    )
    assert len(persisted) == 1
    assert float(persisted[0].value) == 62


def test_provisional_sdk_payload_keeps_opaque_extensions_and_worker_validation_path(db: Session) -> None:
    from app.services.sdk.wearable_history import sanitize_sdk_payload

    member, source = frozen_source(db)
    source.freeze_at = datetime.now(UTC) + timedelta(days=1)
    db.flush()
    raw = {"provider": "apple", "data": {"sleep": [{"values": [{"private": "held"}]}]}}
    with patch("sentry_sdk.capture_message") as capture:
        assert sanitize_sdk_payload(db, member.id, raw) is raw
    capture.assert_not_called()
