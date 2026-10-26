"""FORK migration 51fa8d7eba23 (Calibra ruling R-2026-10-23-f): the skin temperature already held
is deleted.

Every skin-temperature series is seeded, in the live table and in the archive, before the
migration runs, beside rows that must stay: the same member's other series, and another
member's skin temperature (which goes too, since the ruling is not per member).
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataPointSeriesArchive, SeriesTypeDefinition
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.enums.aggregation_method import AggregationMethod
from tests.factories import DataSourceFactory, UserFactory

PATH = Path(__file__).parents[2] / "migrations/versions/2026_10_26_1500-51fa8d7eba23_skin_temperature_not_kept.py"
T0 = datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)

SKIN = [
    SeriesType.skin_temperature,
    SeriesType.skin_temperature_deviation,
    SeriesType.skin_temperature_trend_deviation,
    SeriesType.garmin_skin_temperature,
]
KEPT = [SeriesType.resting_heart_rate, SeriesType.body_temperature]


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
