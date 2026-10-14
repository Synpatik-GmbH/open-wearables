from datetime import datetime, timezone

from app.database import DbSession
from app.models import UserConnection
from app.services.providers.base_strategy import BaseProviderStrategy, ProviderCapabilities, ProviderCoverage
from app.services.providers.polar.coverage import HEALTH_SCORES, SLEEP_FIELDS, TIMESERIES, WORKOUT_FIELDS
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.oauth import PolarOAuth
from app.services.providers.polar.pull_budget import RATE_LIMITED, claim_pull
from app.services.providers.polar.webhook_handler import PolarWebhookHandler
from app.services.providers.polar.webhook_service import polar_webhook_service
from app.services.providers.polar.workouts import PolarWorkouts


class PolarStrategy(BaseProviderStrategy):
    """Polar provider implementation."""

    def __init__(self):
        super().__init__()
        self.polar_oauth = PolarOAuth(
            user_repo=self.user_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
        )
        self.oauth = self.polar_oauth
        self.workouts = PolarWorkouts(
            workout_repo=self.workout_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        self.data_247 = Polar247Data(
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        self.webhooks = PolarWebhookHandler(workouts=self.workouts, data_247=self.data_247)
        self.webhook_service = polar_webhook_service

    def prepare_pull(self, db: DbSession, connection: UserConnection) -> str | None:
        """FORK (2.96.6.1, spec cause 4, AC6): nothing is pulled until Polar accepted the user."""
        return self.polar_oauth.ensure_registered(db, connection)

    def claim_pull(self, db: DbSession, connection: UserConnection, *, ignore_interval: bool = False) -> bool:
        """FORK (2.96.6.1, spec R1, AC17): at most one pull per connection every PULL_INTERVAL."""
        return claim_pull(db, connection.id, datetime.now(timezone.utc), ignore_interval=ignore_interval)

    def pull_stops_on(self, code: str | None) -> bool:
        """FORK (2.96.6.1, spec R1): a 429 ends the pull."""
        return code == RATE_LIMITED

    @property
    def name(self) -> str:
        return "polar"

    @property
    def api_base_url(self) -> str:
        return "https://www.polaraccesslink.com"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            rest_pull=True, webhook_ping=True, webhook_registration_api=True, webhook_inbound_secret=True
        )

    @property
    def coverage(self) -> ProviderCoverage:
        return ProviderCoverage(
            timeseries=TIMESERIES, workout_fields=WORKOUT_FIELDS, sleep_fields=SLEEP_FIELDS, health_scores=HEALTH_SCORES
        )
