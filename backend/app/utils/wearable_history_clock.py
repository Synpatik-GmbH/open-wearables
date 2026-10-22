"""Wearable history boundary (2.96.9 v0.4); never an app or prompt input."""

import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def freeze_at(connection_day: date) -> datetime:
    return datetime.combine(connection_day + timedelta(days=7), time.min, BERLIN).astimezone(UTC)


def candidate_start(connection_day: date, nights: list[date]) -> date:
    first = connection_day - timedelta(days=28)
    evidence = sorted({d for d in nights if first <= d <= connection_day})
    if not evidence or (connection_day - evidence[-1]).days >= 3:
        return connection_day
    candidate = evidence[0]
    previous = evidence[0]
    for night in evidence[1:]:
        if (night - previous).days > 3:
            candidate = night
        previous = night
    return candidate


def local_instant(instant: datetime, offset: str | None) -> datetime:
    if offset and re.fullmatch(r"[+-]\d{2}:\d{2}", offset):
        hours, minutes = map(int, offset[1:].split(":"))
        if minutes < 60 and hours * 60 + minutes <= 14 * 60:
            delta = timedelta(hours=hours, minutes=minutes)
            return instant.astimezone(timezone(delta if offset[0] == "+" else -delta))
    return instant.astimezone(BERLIN)


def night_placement(instant: datetime, offset: str | None) -> tuple[bool, date]:
    local = local_instant(instant, offset)
    if local.hour >= 18:
        return True, local.date() + timedelta(days=1)
    return local.hour < 6, local.date()


def recorded_night_date(start: datetime | None, end: datetime | None, offset: str | None) -> date | None:
    if start is None or end is None or end <= start:
        return None
    is_night, night = night_placement(start, offset)
    return night if is_night else None
