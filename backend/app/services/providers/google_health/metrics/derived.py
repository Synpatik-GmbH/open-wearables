"""Daily totals computed from two dailyRollUp operands.

The machinery (``DerivedDailyMetric`` and ``GoogleHealth247Data._derived_daily_samples``) stays,
so an upstream metric can be restored by adding it here.

FORK (Calibra 2.72.2, design D-02): basal energy, the only derived total (total-calories less
active energy), is not a spec 2.2 type, so nothing is derived.
"""

from app.schemas.providers.google import DerivedDailyMetric

DERIVED_DAILY_METRICS: tuple[DerivedDailyMetric, ...] = ()
