"""Heart-family metrics.

heart-rate supports rollUp + list; daily-resting-heart-rate (Daily), heart-rate-variability
(Sample) and daily-heart-rate-variability (Daily) are list only. The sample HRV type is read
for its RMSSD only; the daily one is RMSSD only — Google documents its average as an RMSSD,
and its deep-sleep field is a second RMSSD that has no series of its own yet.

FORK (Calibra 2.72.2, design D-02): only the spec 2.2 heart types are kept. run-vo2-max,
vo2-max and the sample's SDNN are not read.

heart-rate rollUp values also carry Min/Max alongside the Avg we take; capturing those
would use the `extra` mechanism once dedicated SeriesTypes exist.
"""

from app.schemas.enums import SeriesType
from app.schemas.providers.google import DataTypeMetric, ListSpec, RollupSpec, TimeShape

HEART_METRICS: tuple[DataTypeMetric, ...] = (
    DataTypeMetric(
        "heart-rate",
        SeriesType.heart_rate,
        value_key="heartRate",
        rollup_spec=RollupSpec("beatsPerMinuteAvg", max_range_days=14),
        list_spec=ListSpec("beatsPerMinute", TimeShape.SAMPLE),
    ),
    DataTypeMetric(
        "daily-resting-heart-rate",
        SeriesType.resting_heart_rate,
        value_key="dailyRestingHeartRate",
        list_spec=ListSpec("beatsPerMinute", TimeShape.DATE, is_daily_total=True),
    ),
    DataTypeMetric(
        "heart-rate-variability",
        SeriesType.heart_rate_variability_rmssd,
        value_key="heartRateVariability",
        # FORK (Calibra 2.72.2, design D-02): RMSSD only; the sample's SDNN is not kept.
        list_spec=ListSpec("rootMeanSquareOfSuccessiveDifferencesMilliseconds", TimeShape.SAMPLE),
    ),
    DataTypeMetric(
        "daily-heart-rate-variability",
        SeriesType.heart_rate_variability_rmssd,
        value_key="dailyHeartRateVariability",
        list_spec=ListSpec(
            "averageHeartRateVariabilityMilliseconds",
            TimeShape.DATE,
            is_daily_total=True,
        ),
    ),
)
