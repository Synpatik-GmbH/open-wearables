"""The opt-in memory profile of a Celery worker process.

Prod's I/O worker grew from ~650 to ~880 MiB between 2026-09-26 and 2026-10-16 and then died; dev
grows ~15 MiB a day since the 0.9.0 move. The profile tells the two possible causes apart:
traced memory growing with RSS is a Python leak, and its ``top`` names the source lines; RSS
growing while traced memory stays flat is the C allocator not handing pages back.
"""

import json
import threading
from collections.abc import Iterator

import pytest

from app.integrations.celery import memory_profile
from app.integrations.celery.memory_profile import MemoryProfiler

# Allocations held across reports, so the profiler has growth to find.
_held: list[bytes] = []


@pytest.fixture(autouse=True)
def _release_held() -> Iterator[None]:
    yield
    _held.clear()


def _reports(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "worker_memory_profile"]


def test_it_logs_nothing_when_off(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler()

    for _ in range(50):
        profiler.task_finished(every=0)

    assert _reports(capsys) == []


def test_it_logs_once_every_n_finished_tasks(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler()

    for _ in range(7):
        profiler.task_finished(every=3)

    assert [report["tasks"] for report in _reports(capsys)] == [3, 6]


def test_a_report_carries_rss_and_traced_memory(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler()

    profiler.task_finished(every=1)

    (report,) = _reports(capsys)
    assert report["rss_mib"] > 0
    assert report["traced_mib"] >= 0
    assert report["traced_peak_mib"] >= report["traced_mib"]


def test_the_top_lines_name_where_memory_grew(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler(top=5)
    profiler.task_finished(every=1)  # the baseline
    capsys.readouterr()

    _held.extend(bytes(1024) for _ in range(4096))  # ~4 MiB, held
    profiler.task_finished(every=1)

    (report,) = _reports(capsys)
    (grown,) = [line for line in report["top"] if "test_memory_profile.py" in line["where"]]
    assert grown["growth_kib"] >= 4096
    assert report["top"][0] == grown  # the largest growth is listed first


def test_concurrent_tasks_are_each_counted_once(capsys: pytest.CaptureFixture[str]) -> None:
    # The I/O worker uses the threads pool, so task_postrun fires on many threads at once.
    profiler = MemoryProfiler()

    def finish_tasks() -> None:
        for _ in range(25):
            profiler.task_finished(every=10)

    threads = [threading.Thread(target=finish_tasks) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(report["tasks"] for report in _reports(capsys)) == list(range(10, 201, 10))


def test_a_forked_child_starts_its_own_count(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # The CPU worker's prefork children inherit the parent's state; each counts on its own.
    profiler = MemoryProfiler()
    profiler.task_finished(every=2)

    monkeypatch.setattr(memory_profile.os, "getpid", lambda: -1)
    profiler.task_finished(every=2)
    profiler.task_finished(every=2)

    assert [(report["pid"], report["tasks"]) for report in _reports(capsys)] == [(-1, 2)]


# ── Wiring: the worker's own profiler, fed by Celery's task_postrun signal ──


def test_the_setting_is_read_from_its_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    monkeypatch.setenv("CELERY_MEMORY_PROFILE_EVERY", "50")

    assert Settings().celery_memory_profile_every == 50


def test_the_profile_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    monkeypatch.delenv("CELERY_MEMORY_PROFILE_EVERY", raising=False)

    assert Settings().celery_memory_profile_every == 0


def test_every_finished_task_reaches_the_worker_profiler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from celery import signals

    from app.config import settings
    from app.integrations.celery import core

    monkeypatch.setattr(core, "_memory_profiler", MemoryProfiler())
    monkeypatch.setattr(settings, "celery_memory_profile_every", 2)

    for _ in range(4):
        signals.task_postrun.send(sender=None)

    assert [report["tasks"] for report in _reports(capsys)] == [2, 4]


def test_the_worker_profiler_is_silent_while_the_setting_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from celery import signals

    from app.config import settings
    from app.integrations.celery import core

    monkeypatch.setattr(core, "_memory_profiler", MemoryProfiler())
    monkeypatch.setattr(settings, "celery_memory_profile_every", 0)

    for _ in range(4):
        signals.task_postrun.send(sender=None)

    assert _reports(capsys) == []
