"""Google Health asks for exactly three scopes (Calibra 2.72.3, design D-06, INV-09; spec 2.1, AC 14)."""

from urllib.parse import parse_qs, urlparse

from app.config import Settings, settings
from app.services.providers.google_health.strategy import GoogleHealthStrategy

THREE_SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
]


def authorize_query() -> dict[str, list[str]]:
    url, _ = GoogleHealthStrategy().oauth._build_auth_url("the-state")
    return parse_qs(urlparse(url).query)


def test_the_default_scope_is_exactly_the_three() -> None:
    """The field's default, not the running settings, which an environment can override."""
    assert Settings.model_fields["google_default_scope"].default.split() == THREE_SCOPES


def test_the_authorize_url_asks_for_the_configured_scope_and_nothing_else() -> None:
    assert authorize_query()["scope"] == [settings.google_default_scope]


def test_the_authorize_url_does_not_carry_earlier_grants_forward() -> None:
    assert "include_granted_scopes" not in authorize_query()


def test_the_authorize_url_keeps_its_other_google_parameters() -> None:
    query = authorize_query()
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["response_type"] == ["code"]
