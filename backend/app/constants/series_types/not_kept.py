"""FORK (Calibra ruling R-2026-10-23-f, on DPO point 9): series that are never stored.

Skin temperature, from any provider, was stored without a purpose and is close to inferring
a woman's cycle. Every write into ``data_point_series`` drops these series, so they are neither
kept nor announced by webhook.
"""

from collections.abc import Iterable
from typing import Any, TypeVar

from app.schemas.enums import SeriesType

NOT_KEPT_SERIES: frozenset[SeriesType] = frozenset(
    {
        SeriesType.skin_temperature,
        SeriesType.skin_temperature_deviation,
        SeriesType.skin_temperature_trend_deviation,
        SeriesType.garmin_skin_temperature,
    }
)

T = TypeVar("T")


def is_kept(sample: Any) -> bool:
    """False for a sample of a series that is never stored."""
    return SeriesType(getattr(sample.series_type, "value", sample.series_type)) not in NOT_KEPT_SERIES


def drop_not_kept(samples: Iterable[T]) -> list[T]:
    """The samples whose series is stored, in their order."""
    return [s for s in samples if is_kept(s)]
