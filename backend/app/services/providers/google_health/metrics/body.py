"""Body-measurement metrics.

FORK (Calibra 2.72.2, design D-02): spec 2.2 keeps no body measurement from Google, so weight,
body fat, core body temperature and blood glucose are not read.
"""

from app.schemas.providers.google import DataTypeMetric

BODY_METRICS: tuple[DataTypeMetric, ...] = ()
