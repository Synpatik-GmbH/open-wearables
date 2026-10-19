"""The worker container's start script, ``scripts/start/worker.sh``.

Without ``CELERY_QUEUES`` the script runs two Celery workers in one container: an I/O worker
(``default``, ``sdk_sync``, ``garmin_sync``, ``webhook_sync``) and a CPU worker (``xml_sync``).
The container must stop when EITHER of them exits, so that Azure Container Apps restarts it. On
2026-10-16 the I/O worker died while the CPU worker kept the container ``Running``: nothing
consumed ``sdk_sync`` or ``default`` for 2.7 days, and every Apple Health upload waited in Redis.

The tests run the real script with a fake ``uv`` first on ``PATH``. The fake stands in for one
worker, picked by the ``-n io@`` / ``-n cpu@`` argument: it either exits with a given status or
runs until it is sent SIGTERM, and it logs both to a file.

``wait -n`` needs bash 4.3; macOS ships 3.2 as ``/bin/bash``, so the tests are skipped there. CI
and the image both run bash 5.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "start" / "worker.sh"

_FAKE_UV = """#!/bin/bash
name=cpu
case "$*" in *"-n io@"*) name=io ;; esac
if [ "$name" = io ]; then mode="$FAKE_IO"; else mode="$FAKE_CPU"; fi
echo "$name start" >> "$FAKE_LOG"
trap 'echo "$name term" >> "$FAKE_LOG"; exit 143' TERM
case "$mode" in
    exit:*) echo "$name exit" >> "$FAKE_LOG"; exit "${mode#exit:}" ;;
    *) while true; do sleep 0.05; done ;;
esac
"""


def _bash_has_wait_n() -> bool:
    probe = subprocess.run(["/bin/bash", "-c", "sleep 0 & wait -n"], capture_output=True)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _bash_has_wait_n(), reason="/bin/bash has no `wait -n` (bash < 4.3)")


def _start(tmp_path: Path, *, io: str, cpu: str, queues: str | None = None) -> tuple[subprocess.Popen[bytes], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(_FAKE_UV)
    fake_uv.chmod(0o755)
    log = tmp_path / "workers.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_IO": io,
        "FAKE_CPU": cpu,
        "FAKE_LOG": str(log),
    }
    if queues is not None:
        env["CELERY_QUEUES"] = queues
    proc = subprocess.Popen(["/bin/bash", str(_SCRIPT)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, log


def _wait_for(log: Path, line: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if line in log.read_text().splitlines():
            return
        time.sleep(0.05)
    raise AssertionError(f"{line!r} never logged; log was {log.read_text()!r}")


def _exit_status(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> int:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(f"worker.sh still running {timeout}s after a worker exited") from None


@pytest.mark.parametrize(("dies", "survives"), [("io", "cpu"), ("cpu", "io")])
def test_the_container_stops_when_either_worker_exits(tmp_path: Path, dies: str, survives: str) -> None:
    modes = {dies: "exit:3", survives: "run"}
    proc, log = _start(tmp_path, io=modes["io"], cpu=modes["cpu"])

    status = _exit_status(proc)

    assert status != 0
    # The survivor is stopped rather than orphaned, so a warm shutdown gets the chance to run.
    _wait_for(log, f"{survives} term")


def test_the_container_stops_even_when_a_worker_exits_cleanly(tmp_path: Path) -> None:
    # A worker that exits 0 is still gone; the container must not keep running half-staffed.
    proc, _ = _start(tmp_path, io="exit:0", cpu="run")

    assert _exit_status(proc) != 0


def test_the_container_keeps_running_while_both_workers_run(tmp_path: Path) -> None:
    proc, log = _start(tmp_path, io="run", cpu="run")
    try:
        _wait_for(log, "io start")
        _wait_for(log, "cpu start")
        time.sleep(1)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_sigterm_reaches_both_workers(tmp_path: Path) -> None:
    proc, log = _start(tmp_path, io="run", cpu="run")
    _wait_for(log, "io start")
    _wait_for(log, "cpu start")

    proc.send_signal(signal.SIGTERM)
    _exit_status(proc)

    _wait_for(log, "io term")
    _wait_for(log, "cpu term")


def test_a_dedicated_worker_runs_only_its_own_queues(tmp_path: Path) -> None:
    proc, log = _start(tmp_path, io="exit:0", cpu="run", queues="webhook_sync")

    assert _exit_status(proc) == 0
    assert log.read_text().splitlines() == ["io start", "io exit"]
