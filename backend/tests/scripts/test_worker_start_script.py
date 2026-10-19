"""The worker container's start script, ``scripts/start/worker.sh``.

Without ``CELERY_QUEUES`` the script runs two Celery workers in one container: an I/O worker
(``default``, ``sdk_sync``, ``garmin_sync``, ``webhook_sync``) and a CPU worker (``xml_sync``).
The container must stop when EITHER of them exits, so that Azure Container Apps restarts it. On
2026-10-16 the I/O worker died while the CPU worker kept the container ``Running``: nothing
consumed ``sdk_sync`` or ``default`` for 2.7 days, and every Apple Health upload waited in Redis.

Stopping must also be bounded: Celery's TERM is a warm shutdown that waits for running tasks, and
an ``xml_sync`` task has no time limit. A worker still running ``WORKER_STOP_GRACE_SECONDS`` after
TERM is killed.

The tests run the real script with a fake ``uv`` first on ``PATH``. The fake stands in for one
worker, picked by the ``-n io@`` / ``-n cpu@`` argument: it exits with a given status, runs until
it is sent SIGTERM, or ignores SIGTERM. It logs its pid and what happened to a file; every fake
still alive when a test ends is killed, and none outlives 60 seconds on its own.

``wait -n`` needs bash 4.3; macOS ships 3.2 as ``/bin/bash``, so the tests are skipped there. CI
and the image both run bash 5.
"""

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "start" / "worker.sh"

_FAKE_UV = """#!/bin/bash
name=cpu
case "$*" in *"-n io@"*) name=io ;; esac
if [ "$name" = io ]; then mode="$FAKE_IO"; else mode="$FAKE_CPU"; fi
echo "$name start" >> "$FAKE_LOG"
echo "$name pid $$" >> "$FAKE_LOG"
if [ "$mode" = ignore-term ]; then
    trap 'echo "$name term" >> "$FAKE_LOG"' TERM
else
    trap 'echo "$name term" >> "$FAKE_LOG"; exit 143' TERM
fi
case "$mode" in
    exit:*) echo "$name exit" >> "$FAKE_LOG"; exit "${mode#exit:}" ;;
    *) for _ in $(seq 1200); do command -p sleep 0.05; done ;;
esac
"""


def _bash_has_wait_n() -> bool:
    probe = subprocess.run(["/bin/bash", "-c", "sleep 0 & wait -n"], capture_output=True)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _bash_has_wait_n(), reason="/bin/bash has no `wait -n` (bash < 4.3)")


def _fake_pids(log: Path) -> list[int]:
    if not log.exists():
        return []
    return [int(line.split()[2]) for line in log.read_text().splitlines() if line.split()[1:2] == ["pid"]]


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


@pytest.fixture(autouse=True)
def _kill_leftover_fakes(tmp_path: Path) -> Iterator[None]:
    yield
    for pid in _fake_pids(tmp_path / "workers.log"):
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


def _start(
    tmp_path: Path,
    *,
    io: str,
    cpu: str,
    queues: str | None = None,
    grace: str | None = "1",
    broken_sleep: bool = False,
) -> tuple[subprocess.Popen[bytes], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(_FAKE_UV)
    fake_uv.chmod(0o755)
    if broken_sleep:
        # The script's own `sleep` fails; the fakes call the real one through `command -p`.
        failing = bin_dir / "sleep"
        failing.write_text("#!/bin/bash\nexit 1\n")
        failing.chmod(0o755)
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
    if grace is not None:
        env["WORKER_STOP_GRACE_SECONDS"] = grace
    with (tmp_path / "stderr.log").open("wb") as stderr:
        proc = subprocess.Popen(["/bin/bash", str(_SCRIPT)], env=env, stdout=subprocess.DEVNULL, stderr=stderr)
    return proc, log


def _wait_for(log: Path, line: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if line in log.read_text().splitlines():
            return
        time.sleep(0.05)
    raise AssertionError(f"{line!r} never logged; log was {log.read_text()!r}")


def _pid_of(log: Path, name: str) -> int:
    _wait_for(log, f"{name} start")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for line in log.read_text().splitlines():
            if line.startswith(f"{name} pid "):
                return int(line.split()[2])
        time.sleep(0.05)
    raise AssertionError(f"{name} never logged its pid")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop the script through its own SIGTERM trap, so it stops its workers too."""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _exit_status(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> int:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(proc)
        raise AssertionError(f"worker.sh still running {timeout}s after a worker exited") from None


@pytest.mark.parametrize(("dies", "survives"), [("io", "cpu"), ("cpu", "io")])
def test_the_container_stops_when_either_worker_exits(tmp_path: Path, dies: str, survives: str) -> None:
    modes = {dies: "exit:3", survives: "run"}
    # A long grace: a survivor that stops on TERM must not hold the restart for the whole grace.
    proc, log = _start(tmp_path, io=modes["io"], cpu=modes["cpu"], grace="10")

    status = _exit_status(proc, timeout=3)

    assert status != 0
    # The survivor is stopped rather than orphaned, so a warm shutdown gets the chance to run.
    _wait_for(log, f"{survives} term")


def test_the_container_stops_even_when_a_worker_exits_cleanly(tmp_path: Path) -> None:
    # A worker that exits 0 is still gone; the container must not keep running half-staffed.
    proc, _ = _start(tmp_path, io="exit:0", cpu="run")

    assert _exit_status(proc) != 0


@pytest.mark.parametrize(("dies", "survives"), [("io", "cpu"), ("cpu", "io")])
def test_a_survivor_that_ignores_term_is_killed_after_the_grace(tmp_path: Path, dies: str, survives: str) -> None:
    # Celery's TERM waits for running tasks, and an xml_sync task has no time limit.
    modes = {dies: "exit:3", survives: "ignore-term"}
    proc, log = _start(tmp_path, io=modes["io"], cpu=modes["cpu"], grace="1")
    survivor = _pid_of(log, survives)

    assert _exit_status(proc, timeout=5) != 0
    _wait_for(log, f"{survives} term")
    assert not _alive(survivor)


def test_sigterm_is_bounded_by_the_grace_too(tmp_path: Path) -> None:
    proc, log = _start(tmp_path, io="ignore-term", cpu="ignore-term", grace="1")
    io, cpu = _pid_of(log, "io"), _pid_of(log, "cpu")

    proc.send_signal(signal.SIGTERM)

    assert _exit_status(proc, timeout=5) == 143
    assert not _alive(io)
    assert not _alive(cpu)


@pytest.mark.parametrize("grace", ["abc", "-5", "1.5", "30s"])
def test_a_grace_that_is_not_whole_seconds_falls_back_to_the_default(tmp_path: Path, grace: str) -> None:
    # `sleep` would reject these, and a failed sleep must not cancel the kill.
    proc, log = _start(tmp_path, io="exit:3", cpu="ignore-term", grace=grace)
    survivor = _pid_of(log, "cpu")
    try:
        _wait_for(log, "cpu term")
        time.sleep(2)
        assert proc.poll() is None  # not killed at once: the default grace runs
        assert _alive(survivor)
        stderr = (tmp_path / "stderr.log").read_text()
        assert f"WORKER_STOP_GRACE_SECONDS='{grace}' is not a whole number of seconds; using 30" in stderr
    finally:
        os.kill(survivor, signal.SIGKILL)
        _exit_status(proc)


def test_a_grace_timer_that_cannot_sleep_kills_at_once(tmp_path: Path) -> None:
    # Fail toward stopping: a broken timer must not leave the survivor waited on for ever.
    proc, log = _start(tmp_path, io="exit:3", cpu="ignore-term", grace="30", broken_sleep=True)
    survivor = _pid_of(log, "cpu")

    assert _exit_status(proc, timeout=5) != 0
    assert not _alive(survivor)


def test_a_worker_gets_the_default_grace_before_it_is_killed(tmp_path: Path) -> None:
    # Unset, the grace is long enough for a warm shutdown: the survivor is not killed at once.
    proc, log = _start(tmp_path, io="exit:3", cpu="ignore-term", grace=None)
    survivor = _pid_of(log, "cpu")
    try:
        _wait_for(log, "cpu term")
        time.sleep(2)
        assert proc.poll() is None
        assert _alive(survivor)
    finally:
        # End the grace the way a worker finishing its task would, so the script and its timer
        # exit normally; a TERM here would start a second 30-second grace.
        os.kill(survivor, signal.SIGKILL)
        _exit_status(proc)


def test_the_container_keeps_running_while_both_workers_run(tmp_path: Path) -> None:
    proc, log = _start(tmp_path, io="run", cpu="run")
    try:
        _wait_for(log, "io start")
        _wait_for(log, "cpu start")
        time.sleep(1)
        assert proc.poll() is None
    finally:
        _stop(proc)


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
    assert [line for line in log.read_text().splitlines() if " pid " not in line] == ["io start", "io exit"]
