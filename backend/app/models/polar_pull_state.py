from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel


class PolarPullState(BaseDbModel):
    """FORK (2.96.6.1, spec R1): what keeps Calibra's Polar calls inside Polar's daily limit.

    One row per Polar connection, written by ``app.services.providers.polar.pull_budget``:

    - ``pull_started_at``: when the last pull for the connection started, or null before the
      first. A pull that starts less than ``PULL_INTERVAL`` later is skipped.
    - ``full_fetched_at``: for each data type with a full form, when it was last fetched in
      full without failing, as an ISO instant. A type is fetched in full again once that is
      ``FULL_INTERVAL`` old; until then each pull fetches its short form, or nothing.
    - ``full_retry`` (spec cause 3, AC4): for each data type whose full fetch failed, how many
      times in a row (``failures``) and when it may be asked for again (``next``, an ISO instant).
      Cleared for the type by its next full fetch that succeeds. Read by ``full_due``.
    - ``on_open_at``, ``on_open_day``, ``on_open_count`` (spec R2): when Polar was last asked for
      last night because the member opened the app, the Berlin day of that, and how many times
      that day. Read by ``claim_on_open``.

    Identifiers and state only (R-2026-10-13-e): times and data-type names, no health value.
    Deleted with the connection, which is deleted with the user.
    """

    __tablename__ = "polar_pull_state"

    user_connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_connection.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pull_started_at: Mapped[datetime | None]
    full_fetched_at: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    full_retry: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    on_open_at: Mapped[datetime | None]
    on_open_day: Mapped[date | None]
    on_open_count: Mapped[int] = mapped_column(server_default=text("0"))
