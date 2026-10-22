"""2.96.9.2 D-02: no raw payload, SDK or FIT copy is kept longer than seven days, for any member."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.celery.core import create_celery
from app.integrations.celery.tasks.wearable_history_purge_task import sweep_expired_copies_task
from app.services import raw_payload_storage

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def client_with(objects: dict[str, list[tuple[str, datetime]]]) -> MagicMock:
    client = MagicMock()

    def paginate(Bucket: str, Prefix: str) -> list[dict]:  # noqa: N803 - boto3's keyword names
        return [{"Contents": [{"Key": k, "LastModified": t} for k, t in objects.get(Prefix, [])]}]

    client.get_paginator.return_value.paginate.side_effect = paginate
    return client


def test_sweep_deletes_only_copies_older_than_seven_days_under_every_copy_prefix() -> None:
    old = NOW - timedelta(days=7, seconds=1)
    young = NOW - timedelta(days=6, hours=23)
    client = client_with(
        {
            "raw-payloads/": [
                ("raw-payloads/polar/webhook/2026-11-03/_unknown/a.json", old),
                ("raw-payloads/apple/sdk/2026-11-04/u/b.json", young),
            ],
            "fit-files/": [("fit-files/garmin/2026-11-02/u/1.fit", old)],
        }
    )
    with patch.multiple(raw_payload_storage, _s3_client=client, _s3_bucket="bucket", _s3_prefix="raw-payloads"):
        assert raw_payload_storage.sweep_expired_copies(NOW) == 2
    deleted = sorted(c.kwargs["Key"] for c in client.delete_object.call_args_list)
    assert deleted == ["fit-files/garmin/2026-11-02/u/1.fit", "raw-payloads/polar/webhook/2026-11-03/_unknown/a.json"]


def test_a_failed_deletion_is_logged_and_raised_after_the_others_are_deleted(capsys: pytest.CaptureFixture) -> None:
    old = NOW - timedelta(days=8)
    client = client_with({"raw-payloads/": [("raw-payloads/x.json", old), ("raw-payloads/y.json", old)]})

    def delete(**kwargs: str) -> None:
        if kwargs["Key"].endswith("x.json"):
            raise RuntimeError("denied")

    client.delete_object.side_effect = delete
    with (
        patch.multiple(raw_payload_storage, _s3_client=client, _s3_bucket="bucket", _s3_prefix="raw-payloads"),
        pytest.raises(RuntimeError, match="1 of 2 expired copies"),
    ):
        raw_payload_storage.sweep_expired_copies(NOW)
    assert [c.kwargs["Key"] for c in client.delete_object.call_args_list] == [
        "raw-payloads/x.json",
        "raw-payloads/y.json",
    ]
    logged = capsys.readouterr().out
    assert logged.count("Expired copy deletion failed") == 1
    assert "raw-payloads/x.json" in logged


def test_sweep_is_a_no_op_without_object_storage() -> None:
    with patch.multiple(raw_payload_storage, _s3_client=None, _s3_bucket=None):
        assert raw_payload_storage.sweep_expired_copies(NOW) == 0


def test_the_sweep_is_scheduled_every_fifteen_minutes_and_runs_the_retention_sweep() -> None:
    entry = create_celery().conf.beat_schedule["sweep-expired-copies"]
    assert entry["task"] == sweep_expired_copies_task.name
    assert entry["schedule"].minute == set(range(0, 60, 15))
    with patch(
        "app.integrations.celery.tasks.wearable_history_purge_task.sweep_expired_copies", return_value=3
    ) as sweep:
        assert sweep_expired_copies_task.run() == {"deleted": 3}
    sweep.assert_called_once_with()
