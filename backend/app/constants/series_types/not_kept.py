"""FORK (Calibra rulings R-2026-10-23-f and R-2026-10-26-d): what is never stored.

Skin temperature (DPO point 9: stored without a purpose and close to inferring a woman's cycle)
and, from every source, body temperature. Every write into ``data_point_series`` drops these
series, so they are neither kept nor announced by webhook, and every health-score write drops
the score components that carry them. Environmental temperatures (water, weather, air) are not
the member's and are kept.
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
        SeriesType.body_temperature,
    }
)

# Score components that carry a temperature: WHOOP recovery's skin temperature, and Oura
# readiness's body-temperature contributor.
NOT_KEPT_COMPONENTS: frozenset[str] = frozenset({"skin_temp_celsius", "body_temperature"})

T = TypeVar("T")


def is_kept(sample: Any) -> bool:
    """False for a sample of a series that is never stored."""
    return SeriesType(getattr(sample.series_type, "value", sample.series_type)) not in NOT_KEPT_SERIES


def drop_not_kept(samples: Iterable[T]) -> list[T]:
    """The samples whose series is stored, in their order."""
    return [s for s in samples if is_kept(s)]


def without_not_kept_components(score: T) -> T:
    """The health score with no component that is never stored."""
    components = getattr(score, "components", None)
    if not components or not NOT_KEPT_COMPONENTS & set(components):
        return score
    kept = {k: v for k, v in components.items() if k not in NOT_KEPT_COMPONENTS}
    return score.model_copy(update={"components": kept or None})  # ty:ignore[unresolved-attribute]
