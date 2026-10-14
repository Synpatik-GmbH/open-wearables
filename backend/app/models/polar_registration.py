from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import int_zero, str_64


class PolarRegistration(BaseDbModel):
    """FORK (2.96.6.1, spec cause 4, L1): whether Polar accepted this connection's user registration.

    Polar serves no data for a user that was never registered with the partner app, and the
    registration call after OAuth used to swallow every failure. One row per Polar connection:

    - ``registered`` true: Polar answered 2xx, or 409 (already registered).
    - ``registered`` false: the last attempt failed; it is retried at the start of every pull,
      the pull fetches nothing for the connection until it succeeds, and the connections API
      reports ``registration_pending``.
    - no row: never attempted under this code (a connection made before 2.96.6.1). The next
      pull attempts it, so a registration that failed silently before the fix is retried too.

    Identifiers and state only (R-2026-10-13-e): ``last_error_code`` is a code from
    ``app.services.sync_error_code.error_code``, never a message. Deleted with the connection, which is deleted with
    the user.
    """

    __tablename__ = "polar_registration"

    user_connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_connection.id", ondelete="CASCADE"),
        primary_key=True,
    )
    registered: Mapped[bool]
    attempts: Mapped[int_zero]
    last_error_code: Mapped[str_64 | None]
    updated_at: Mapped[datetime]
