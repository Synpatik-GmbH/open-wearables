"""Vitals metrics (respiratory rate) — list-only types.

daily-respiratory-rate is a Daily type (date-stamped) and does not support rollUp; its unit
already matches (brpm).

FORK (Calibra 2.72.2, design D-02): oxygen saturation, in both its Sample and Daily shapes,
is not read.
"""

from app.schemas.enums import SeriesType
from app.schemas.providers.google import DataTypeMetric, ListSpec, TimeShape

VITALS_METRICS: tuple[DataTypeMetric, ...] = (
    DataTypeMetric(
        "daily-respiratory-rate",
        SeriesType.respiratory_rate,
        value_key="dailyRespiratoryRate",
        list_spec=ListSpec("breathsPerMinute", TimeShape.DATE, is_daily_total=True),
    ),
)
