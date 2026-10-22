from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Date, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey, str_64


class WearableHistorySource(BaseDbModel):
    """First-connection lifetime; deliberately independent of UserConnection."""

    __tablename__ = "wearable_history_sources"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_wearable_history_user_provider"),)

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    provider: Mapped[str_64]
    connection_day: Mapped[date]
    start_date: Mapped[date]
    freeze_at: Mapped[datetime]
    night_dates: Mapped[list[date]] = mapped_column(ARRAY(Date), nullable=False, default=list)
