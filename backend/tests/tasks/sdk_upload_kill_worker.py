"""A real Celery worker for ``test_process_sdk_upload_worker_kill.py`` (2.96.6.3), run as
``python -m tests.tasks.sdk_upload_kill_worker`` in its own process so the test can SIGKILL it.

It is the I/O worker's shape (``--pool=threads``) on the ``sdk_sync`` queue, with two switches
read from the environment:

- ``SDK_UPLOAD_HOLD_FILE``: the import touches this file and then blocks, so the test knows the
  batch is in the middle of its delivery when it kills the worker. Nothing is written before.
- ``SDK_UPLOAD_VISIBILITY_TIMEOUT``: seconds, in place of Kombu's hour, so a restarted worker takes
  back the killed worker's unacknowledged batch at once.
"""

import os
import time
from pathlib import Path
from typing import Any


def _hold_import(hold_file: str) -> None:
    from app.services.sdk.import_service import ImportService

    def held(*_args: Any, **_kwargs: Any) -> Any:
        Path(hold_file).touch()
        while True:
            time.sleep(60)

    ImportService.import_data_from_request = held  # ty:ignore[invalid-assignment]


def main() -> None:
    from app.main import celery_app

    hold_file = os.environ.get("SDK_UPLOAD_HOLD_FILE")
    if hold_file:
        _hold_import(hold_file)
    visibility_timeout = os.environ.get("SDK_UPLOAD_VISIBILITY_TIMEOUT")
    if visibility_timeout:
        celery_app.conf.broker_transport_options = {
            **celery_app.conf.broker_transport_options,
            "visibility_timeout": int(visibility_timeout),
        }
    celery_app.worker_main(
        [
            "worker",
            "--pool=threads",
            "--concurrency=1",
            "-Q",
            "sdk_sync",
            "-n",
            f"sdk-upload-kill-{os.getpid()}@%h",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=INFO",
        ]
    )


if __name__ == "__main__":
    main()
