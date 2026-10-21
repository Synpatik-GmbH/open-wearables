from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class WearableHistorySourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    connection_day: date
    start_date: date
    freeze_at: datetime
