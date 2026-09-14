"""The one-shot operator script that migrates Svix endpoints off readable user channels.

Its whole contract is the exit code and the report: an operator runs it once per environment after
upgrading and must be able to tell "nothing to migrate" from "could not reach Svix".
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.outgoing_webhooks import svix as svix_service

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "pseudonymise_svix_user_channels.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pseudonymise_svix_user_channels", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_module()


def test_success_reports_the_counts_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    result = svix_service.LegacyChannelMigration(applications=2, endpoints=5, legacy=1, migrated=1)
    with (
        patch.object(script.svix_service, "is_enabled", return_value=True),
        patch.object(script.svix_service, "migrate_legacy_user_channels", return_value=result) as run,
    ):
        assert script.main([]) == 0
    run.assert_called_once_with(dry_run=False)
    out = capsys.readouterr().out
    for expected in ("applications: 2", "endpoints: 5", "legacy: 1", "migrated: 1"):
        assert expected in out


def test_dry_run_is_passed_through(capsys: pytest.CaptureFixture[str]) -> None:
    result = svix_service.LegacyChannelMigration(applications=1, endpoints=1, legacy=1, migrated=0)
    with (
        patch.object(script.svix_service, "is_enabled", return_value=True),
        patch.object(script.svix_service, "migrate_legacy_user_channels", return_value=result) as run,
    ):
        assert script.main(["--dry-run"]) == 0
    run.assert_called_once_with(dry_run=True)
    assert "dry run" in capsys.readouterr().out.lower()


def test_a_svix_failure_exits_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(script.svix_service, "is_enabled", return_value=True),
        patch.object(script.svix_service, "migrate_legacy_user_channels", side_effect=httpx.ConnectError("svix down")),
    ):
        assert script.main([]) == 1
    assert "rerun" in capsys.readouterr().out.lower()


def test_svix_not_configured_exits_non_zero() -> None:
    with (
        patch.object(script.svix_service, "is_enabled", return_value=False),
        patch.object(script.svix_service, "migrate_legacy_user_channels") as run,
    ):
        assert script.main([]) == 1
    run.assert_not_called()


def test_the_script_drives_the_real_migration() -> None:
    """Not only a wrapper around a mock: through the real function against a mocked Svix client."""
    uid_channel = "user.11de240a-4e95-4eed-ae00-983f34dcbd3b"
    client = MagicMock()
    client.application.list.return_value = MagicMock(data=[MagicMock(id="app_1")], done=True, iterator=None)
    client.endpoint.list.return_value = MagicMock(
        data=[MagicMock(id="ep_1", channels=[uid_channel])], done=True, iterator=None
    )
    with patch.object(svix_service, "_client", client):
        assert script.main([]) == 0
    client.endpoint.patch.assert_called_once()
