"""FORK migration 51fa8d7eba23 (Calibra ruling R-2026-10-23-f): the skin temperature already held
is deleted.

Every skin-temperature series is seeded, in the live table and in the archive, before the
migration runs, beside rows that must stay: the same member's other series, and another
member's skin temperature (which goes too, since the ruling is not per member).
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataPointSeriesArchive, HealthScore, SeriesTypeDefinition
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import ProviderName, SeriesType, get_series_type_id
from app.schemas.enums.aggregation_method import AggregationMethod
from app.schemas.enums.health_score_category import HealthScoreCategory
from tests.factories import DataSourceFactory, UserFactory

PATH = Path(__file__).parents[2] / "migrations/versions/2026_10_26_1500-51fa8d7eba23_skin_temperature_not_kept.py"
T0 = datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)

SKIN = [
    SeriesType.skin_temperature,
    SeriesType.skin_temperature_deviation,
    SeriesType.skin_temperature_trend_deviation,
    SeriesType.garmin_skin_temperature,
    SeriesType.body_temperature,
]
KEPT = [SeriesType.resting_heart_rate, SeriesType.weather_temperature]


def migration() -> object:
    spec = importlib.util.spec_from_file_location("skin_temperature_not_kept", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(db: Session, source_id: object, series: SeriesType) -> None:
    db.add(
        DataPointSeries(
            id=uuid4(),
            data_source_id=source_id,
            recorded_at=T0,
            value=33.6,
            series_type_definition_id=get_series_type_id(series),
        )
    )
    db.add(
        DataPointSeriesArchive(
            id=uuid4(),
            data_source_id=source_id,
            series_type_definition_id=get_series_type_id(series),
            bucket_start_at=T0,
            aggregation_type=AggregationMethod.AVG,
            value=33.6,
            sample_count=1,
        )
    )


def _codes(db: Session, model: type) -> list[str]:
    rows = db.execute(
        select(SeriesTypeDefinition.code).join(model, model.series_type_definition_id == SeriesTypeDefinition.id)
    )
    return sorted(code for (code,) in rows)


def test_held_skin_temperature_is_deleted_from_the_series_and_the_archive_and_the_rest_stays(db: Session) -> None:
    member, other = UserFactory(), UserFactory()
    polar = DataSourceFactory(user=member, source="polar")
    whoop = DataSourceFactory(user=other, source="whoop")
    db.flush()
    for series in [*SKIN, *KEPT]:
        _seed(db, polar.id, series)
    _seed(db, whoop.id, SeriesType.skin_temperature)
    db.flush()

    with Operations.context(MigrationContext.configure(db.connection())):
        migration().upgrade()

    kept = sorted(s.value for s in KEPT)
    assert _codes(db, DataPointSeries) == kept
    assert _codes(db, DataPointSeriesArchive) == kept


def test_a_held_score_loses_its_temperature_components_and_keeps_the_rest(db: Session) -> None:
    member = UserFactory()
    db.flush()
    held = {"skin_temp_celsius": {"value": 33.6}, "resting_heart_rate": {"value": 52}}
    oura = {"body_temperature": {"value": 60}, "hrv_balance": {"value": 75}}
    only = {"skin_temp_celsius": {"value": 34.0}}
    other = {"resting_heart_rate": {"value": 50}}
    for day, components in enumerate((held, oura, only, other, None)):
        db.add(
            HealthScore(
                id=uuid4(),
                user_id=member.id,
                provider=ProviderName.WHOOP,
                category=HealthScoreCategory.RECOVERY,
                value=70,
                recorded_at=T0 + timedelta(days=day),
                components=components,
            )
        )
    db.flush()

    with Operations.context(MigrationContext.configure(db.connection())):
        migration().upgrade()
    db.expire_all()

    stored = sorted(
        (str(c) for (c,) in db.execute(select(HealthScore.components).where(HealthScore.user_id == member.id))),
    )
    # A score whose only component was a temperature has none left: NULL, as a new write stores it.
    kept = [{"resting_heart_rate": {"value": 52}}, {"hrv_balance": {"value": 75}}, None, other, None]
    assert stored == sorted(str(c) for c in kept)


def test_a_score_left_with_no_component_is_stored_as_a_new_write_stores_none(db: Session) -> None:
    """The ORM stores a score with no components as JSON null; the migration must store the same."""
    member = UserFactory()
    db.flush()
    for day, components in enumerate(({"skin_temp_celsius": {"value": 34.0}}, None)):
        db.add(
            HealthScore(
                id=uuid4(),
                user_id=member.id,
                provider=ProviderName.WHOOP,
                category=HealthScoreCategory.RECOVERY,
                value=70,
                recorded_at=T0 + timedelta(days=day),
                components=components,
            )
        )
    db.flush()

    with Operations.context(MigrationContext.configure(db.connection())):
        migration().upgrade()

    stored = (
        db.execute(
            text(
                "SELECT coalesce(jsonb_typeof(components), 'sql null') FROM health_score "
                "WHERE user_id = :u ORDER BY recorded_at"
            ),
            {"u": member.id},
        )
        .scalars()
        .all()
    )
    assert stored[0] == stored[1]


@pytest.mark.parametrize("source_provider", ["whoop", "polar"])
def test_the_component_strip_runs_past_the_wearable_history_guard(db: Session, source_provider: str) -> None:
    """A held score the history guard would now refuse (pre-start for a frozen WHOOP source, or a
    WHOOP score for a member whose only source is another provider) still loses its temperature.

    Removing a component only reduces what is held, so the migration must not abort on the guard.
    """
    member = UserFactory()
    db.flush()
    db.add(
        HealthScore(
            id=uuid4(),
            user_id=member.id,
            provider=ProviderName.WHOOP,
            category=HealthScoreCategory.RECOVERY,
            value=70,
            recorded_at=datetime.now(timezone.utc) - timedelta(days=90),
            components={"skin_temp_celsius": {"value": 34.0}, "resting_heart_rate": {"value": 52}},
        )
    )
    db.flush()
    db.execute(text("SET CONSTRAINTS wh_ingress IMMEDIATE"))
    source = WearableHistoryRepository().begin_source(db, member.id, source_provider)
    source.freeze_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()

    with Operations.context(MigrationContext.configure(db.connection())):
        migration().upgrade()

    stored = db.scalar(select(HealthScore.components).where(HealthScore.user_id == member.id))
    assert stored == {"resting_heart_rate": {"value": 52}}
