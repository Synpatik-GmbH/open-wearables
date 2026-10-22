from datetime import datetime


def interval_gap(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> float:
    if start <= other_end and end >= other_start:
        return 0.0
    return (other_start - end).total_seconds() if end <= other_start else (start - other_end).total_seconds()
