from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class WearableHistorySourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    connection_day: date
    start_date: date
    freeze_at: datetime
    # 2.96.9.2 D-03: when this source's records purge committed, and when its whole purge completed.
    records_purged_at: datetime | None = None
    purge_completed_at: datetime | None = None
