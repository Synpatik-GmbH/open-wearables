"""FORK (Calibra 2.72.2, design D-03): the Google Health history window.

Spec 2.72 v0.2 P5 and AC 1: nothing older than 28 days before the connect day, and nothing before
the history floor, is stored; a request may reach one local day further back. The connect day and
its 28 days are Europe/Berlin dates, as the wearable-history connection day is. Google filters daily
types and exercise by a date, so those requests ask from one day before their cut and the
client-side check decides what is stored; instant-filtered types are requested and cut on the same
instants.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID

from app.database import DbSession
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.enums import ProviderName
from app.utils.wearable_history_clock import BERLIN
from app.utils.wearable_history_ingress import WearableHistoryMetadataUnavailableError

HISTORY_DAYS = 28
_ONE_DAY = timedelta(days=1)


def berlin_today() -> date:
    return datetime.now(BERLIN).date()


def berlin_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, BERLIN)


def connect_pull_start(now: datetime) -> datetime:
    """Where the pull at connect starts: 00:00 Europe/Berlin, 28 days before the connect day."""
    return berlin_midnight(now.astimezone(BERLIN).date() - timedelta(days=HISTORY_DAYS))


@dataclass(frozen=True)
class HistoryWindow:
    """The member's history floor and today's Berlin date, which every Google request is cut by."""

    floor: date
    today: date

    def instant_cut(self, start: datetime) -> datetime:
        """The lower bound of an instant-filtered request and of what it stores."""
        return max(start, berlin_midnight(self.floor))

    def request_date(self, start: datetime) -> date:
        """The lower date of a date-filtered request: one day of slack before its cut."""
        return max(self.floor, start.date()) - _ONE_DAY

    def keep_date(self, start: datetime) -> date:
        """The first date a daily value is stored for.

        The day before the window is kept only while it is within the last 28 days, so a pull
        at connect, whose window starts 28 days back, stores nothing before it.
        """
        back = start.date() - _ONE_DAY
        if back >= self.today - timedelta(days=HISTORY_DAYS):
            return max(self.floor, back)
        return max(self.floor, start.date())


def history_window(db: DbSession, user_id: UUID, connection_repo: UserConnectionRepository) -> HistoryWindow:
    """The member's window: floor from the history row's connection day, else the connection's.

    A reconnect moves neither: the connection day is the first connection's, and a reconnect keeps
    the connection row's ``created_at``. With neither, nothing can be requested safely.
    """
    provider = ProviderName.GOOGLE_HEALTH.value
    source = WearableHistoryRepository().get(db, user_id, provider)
    if source is not None:
        day = source.connection_day
    else:
        connection = connection_repo.get_by_user_and_provider(db, user_id, provider)
        if connection is None:
            raise WearableHistoryMetadataUnavailableError("Google Health history window unavailable")
        day = connection.created_at.astimezone(BERLIN).date()
    return HistoryWindow(floor=day - timedelta(days=HISTORY_DAYS), today=berlin_today())
