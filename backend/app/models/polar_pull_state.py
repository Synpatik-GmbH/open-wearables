from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel


class PolarPullState(BaseDbModel):
    """FORK (2.96.6.1, spec R1): what keeps Calibra's Polar calls inside Polar's daily limit.

    One row per Polar connection, written by ``app.services.providers.polar.pull_budget``:

    - ``pull_started_at``: when the last pull for the connection started. A pull that starts
      less than ``PULL_INTERVAL`` later is skipped.
    - ``full_fetched_at``: for each data type with a full form, when it was last fetched in
      full without failing, as an ISO instant. A type is fetched in full again once that is
      ``FULL_INTERVAL`` old; until then each pull fetches its short form, or nothing.

    Identifiers and state only (R-2026-10-13-e): times and data-type names, no health value.
    Deleted with the connection, which is deleted with the user.
    """

    __tablename__ = "polar_pull_state"

    user_connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_connection.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pull_started_at: Mapped[datetime]
    full_fetched_at: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
