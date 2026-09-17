"""The SDK refresh-token grace window is a validated setting (fork spec 2026-09-11, D-05)."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_default_is_seven_days() -> None:
    assert Settings(secret_key="test").sdk_refresh_grace_seconds == 604800


@pytest.mark.parametrize("value", [0, -1, "7d", "P7D"])
def test_non_positive_or_unparsable_value_fails_startup(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(secret_key="test", sdk_refresh_grace_seconds=value)  # ty: ignore[invalid-argument-type]
