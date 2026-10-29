"""Activity-family metrics (steps).

FORK (Calibra 2.72.2, design D-02): steps is the only activity type spec 2.2 keeps. Distance,
active energy and hydration are not read; total-calories was only an operand of the derived
basal energy, which is not derived either (metrics/derived.py).
"""

from app.schemas.enums import SeriesType
from app.schemas.providers.google import DataTypeMetric, ListSpec, RollupSpec, TimeShape

ACTIVITY_METRICS: tuple[DataTypeMetric, ...] = (
    DataTypeMetric(
        "steps",
        SeriesType.steps,
        value_key="steps",
        rollup_spec=RollupSpec("countSum"),
        list_spec=ListSpec("count", TimeShape.INTERVAL),
    ),
)
