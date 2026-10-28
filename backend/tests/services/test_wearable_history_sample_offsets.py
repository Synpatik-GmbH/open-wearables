"""After the freeze, a first-night sample is kept only inside its own source's stored night.

Otherwise it is dropped, never retried.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.routes.v1.sdk_sync import sync_sdk_data
from app.config import settings
from app.models import DataPointSeries, DataSource, EventRecord, User
from app.repositories.wearable_history_ingress_repository import WearableHistoryIngressRepository
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from tests.factories import DataSourceFactory, EventRecordFactory, UserFactory
from tests.services.test_wearable_history_review_closure import envelope

METRICS = [
    SeriesType.heart_rate,
    SeriesType.heart_rate_variability_sdnn,
    SeriesType.heart_rate_variability_rmssd,
    SeriesType.garmin_hrv_rmssd_nightly,  # Calibra 2.71.3.2
    SeriesType.resting_heart_rate,
    SeriesType.respiratory_rate,
    SeriesType.oxygen_saturation,
    SeriesType.skin_temperature,
    SeriesType.body_temperature,
]
SDK_METRICS = [
    "HEART_RATE",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
    "HEART_RATE_VARIABILITY",
    "RESTING_HEART_RATE",
    "RESPIRATORY_RATE",
    "OXYGEN_SATURATION",
    "BODY_TEMPERATURE",
]
INSTANT = datetime(2026, 10, 12, 15, 30, tzinfo=UTC)


def source_setup(db: Session) -> tuple[User, DataSource]:
    member = UserFactory()
    history = WearableHistoryRepository().begin_source(db, member.id, "apple", datetime(2026, 10, 13, 12, tzinfo=UTC))
    source = DataSourceFactory(user=member, provider="apple", source="apple")
    db.commit()
    assert history.start_date.isoformat() == "2026-10-13"
    return member, source


def own_night(db: Session, source: DataSource, offset: str) -> EventRecord:
    start = datetime(2026, 10, 12, 15 if offset == "+04:00" else 6, tzinfo=UTC)
    return EventRecordFactory(
        data_source=source,
        category="sleep",
        type="sleep_session",
        start_datetime=start,
        end_datetime=datetime(2026, 10, 12, 16, tzinfo=UTC),
        zone_offset=offset,
    )


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("offset", ["+04:00", "+14:00"])
@pytest.mark.parametrize("path", ["repository", "sql"])
def test_first_night_sample_offset_is_rejected_until_its_own_night_is_stored(
    db: Session, metric: SeriesType, offset: str, path: str
) -> None:
    member, source = source_setup(db)
    sample = TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=member.id,
        data_source_id=source.id,
        source="apple",
        recorded_at=INSTANT,
        zone_offset=offset,
        value=61,
        series_type=metric,
    )
    if path == "repository":
        assert WearableHistoryIngressRepository(db).sample_admitted(sample) is False
    else:

        def insert_without_proof() -> None:
            with db.begin_nested():
                db.execute(
                    insert(DataPointSeries).values(
                        id=sample.id,
                        data_source_id=source.id,
                        recorded_at=INSTANT,
                        zone_offset=offset,
                        value=61,
                        series_type_definition_id=get_series_type_id(metric),
                    )
                )
                db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

        with pytest.raises(IntegrityError) as rejected:
            insert_without_proof()
        assert rejected.value.orig.diag.message_primary == "Frozen source rejects pre-start data"
    assert db.get(DataPointSeries, sample.id) is None
    own_night(db, source, offset)
    db.commit()
    assert WearableHistoryIngressRepository(db).sample_admitted(sample)
    db.execute(
        insert(DataPointSeries).values(
            id=sample.id,
            data_source_id=source.id,
            recorded_at=INSTANT,
            zone_offset=offset,
            value=61,
            series_type_definition_id=get_series_type_id(metric),
        )
    )
    db.commit()
    assert db.get(DataPointSeries, sample.id).zone_offset == offset


def test_garmins_nightly_value_is_admitted_by_its_own_night_in_both_database_functions(db: Session) -> None:
    """Calibra 2.71.3.2 (migration 7c2f4e9a1b30): the write and read gates both place Garmin's
    nightly HRV value by the recorded night that contains it, as they place RMSSD. Asked
    directly: the sample tests above prove the Python gate, and their database insert after the
    night is deferred past the end of the test transaction."""
    _member, source = source_setup(db)
    metric = get_series_type_id(SeriesType.garmin_hrv_rmssd_nightly)
    written = text("SELECT wh_sample_admitted(:source,:metric,:instant,:zone)")
    read = text("SELECT wh_read_sample(:source,:metric,:instant)")
    args = dict(source=source.id, metric=metric, instant=INSTANT)

    assert db.scalar(written, {**args, "zone": "+04:00"}) is False
    own_night(db, source, "+04:00")
    db.commit()

    assert db.scalar(written, {**args, "zone": "+04:00"}) is True
    assert db.scalar(read, args) is True


@pytest.mark.parametrize("metric", SDK_METRICS)
@pytest.mark.parametrize("offset", ["+04:00", "+14:00"])
@pytest.mark.parametrize("offload", [False, True])
def test_sdk_sample_offset_is_dropped_before_transport_then_queued_after_proof(
    db: Session, metric: str, offset: str, offload: bool
) -> None:
    member, source = source_setup(db)
    body = envelope(
        {
            "records": [
                {
                    "id": "first-night-sample",
                    "type": metric,
                    "value": 61,
                    "unit": "count",
                    "startDate": INSTANT.isoformat(),
                    "endDate": INSTANT.isoformat(),
                    "zoneOffset": offset,
                }
            ]
        }
    )
    with (
        patch.object(settings, "sdk_payload_s3_offload", offload),
        patch("app.api.routes.v1.sdk_sync.load_sleep_state", return_value=None),
        patch("app.api.routes.v1.sdk_sync.store_raw_payload"),
        patch("app.api.routes.v1.sdk_sync.put_payload_to_s3", return_value="s3://test-bucket/payload") as s3,
        patch("app.api.routes.v1.sdk_sync.process_sdk_upload.delay") as queue,
    ):
        # Dropped, not retried: the upload succeeds without it, so the app's sync moves on.
        response = sync_sdk_data(str(member.id), json.loads(json.dumps(body)), SimpleNamespace(auth_type="api_key"), db)
        assert response.status_code == 202
        payload = json.loads(s3.call_args.kwargs["payload"] if offload else queue.call_args.kwargs["content"])
        assert payload["data"]["records"] == []
        own_night(db, source, offset)
        db.commit()
        response = sync_sdk_data(str(member.id), body, SimpleNamespace(auth_type="api_key"), db)
        assert response.status_code == 202
        payload = json.loads(s3.call_args.kwargs["payload"] if offload else queue.call_args.kwargs["content"])
        assert [r["id"] for r in payload["data"]["records"]] == ["first-night-sample"]
        assert payload["data"]["records"][0]["zoneOffset"] == offset


@pytest.mark.parametrize("offset", [None, "bad", "+14:01", "-04:00"])
def test_nonfirst_night_offset_retains_the_existing_berlin_fallback_or_held_date(
    db: Session, offset: str | None
) -> None:
    member, source = source_setup(db)
    sample = SimpleNamespace(
        user_id=member.id,
        data_source_id=source.id,
        series_type=SeriesType.heart_rate,
        recorded_at=INSTANT,
        zone_offset=offset,
    )
    assert WearableHistoryIngressRepository(db).sample_admitted(sample) is False
    assert (
        db.scalar(
            text("SELECT wh_sample_admitted(:source,:metric,:instant,:zone)"),
            dict(source=source.id, metric=get_series_type_id(SeriesType.heart_rate), instant=INSTANT, zone=offset),
        )
        is False
    )


def test_source_local_first_night_proof_cannot_come_from_another_provider(db: Session) -> None:
    member, source = source_setup(db)
    WearableHistoryRepository().begin_source(db, member.id, "polar", datetime(2026, 10, 13, 12, tzinfo=UTC))
    other = DataSourceFactory(user=member, provider="polar")
    own_night(db, other, "+04:00")
    db.commit()
    sample = SimpleNamespace(
        user_id=member.id,
        data_source_id=source.id,
        series_type=SeriesType.heart_rate,
        recorded_at=INSTANT,
        zone_offset="+04:00",
    )
    assert WearableHistoryIngressRepository(db).sample_admitted(sample) is False
