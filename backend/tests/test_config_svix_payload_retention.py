"""SVIX_PAYLOAD_RETENTION_DAYS is bounded to what svix-server accepts (5 to 90 days).

Outside that range svix-server answers every message.create with a 422, so an unbounded
setting would let the app and workers start normally and then fail every webhook at runtime.
Rejecting it at configuration time turns a silent delivery outage into a failed startup.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_default_is_the_platform_floor() -> None:
    assert Settings(secret_key="test").svix_payload_retention_days == 5


@pytest.mark.parametrize("value", [5, 30, 90])
def test_values_svix_accepts_are_accepted(value: int) -> None:
    assert Settings(secret_key="test", svix_payload_retention_days=value).svix_payload_retention_days == value


@pytest.mark.parametrize("value", [0, 4, 91, -5])
def test_values_svix_rejects_fail_startup(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(secret_key="test", svix_payload_retention_days=value)
