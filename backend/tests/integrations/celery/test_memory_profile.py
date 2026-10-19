"""The memory profile of a Celery worker process.

Prod's I/O worker grew from ~650 to ~880 MiB between 2026-09-26 and 2026-10-16 and then died; dev's
grew from ~535 to ~800 MiB after the 0.9.0 move on 2026-10-05 (Azure Monitor ``WorkingSetBytes``,
read 2026-10-19). The cheap report says which cause it is: the allocator keeping freed memory
(``malloc_free_mib`` grows), or something holding memory (``malloc_in_use_mib`` or ``python_blocks``
grows). The
heavy mode then names the Python lines holding it, or names none, which points at a C library.
"""

import ctypes
import json
import platform
import sys
import threading
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest
from celery import signals

from app.config import Settings, settings
from app.integrations.celery import core, memory_profile
from app.integrations.celery.memory_profile import MemoryProfiler, parse_malloc_info

# Allocations held across reports, so the profiler has growth to find.
_held: list[bytes | str] = []

# Decided apart from read_malloc_info, so a broken reader fails these tests rather than skipping them.
needs_glibc = pytest.mark.skipif(platform.libc_ver()[0] != "glibc", reason="no glibc here")


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    was_tracing = tracemalloc.is_tracing()
    yield
    _held.clear()
    if not was_tracing:
        tracemalloc.stop()


def _reports(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("action") == "worker_memory_profile"]


# ── The malloc_info parse ──

_MALLOC_INFO = """<malloc version="1">
<heap nr="0"><sizes></sizes><total type="fast" count="1" size="64"/></heap>
<heap nr="1"><sizes></sizes><total type="rest" count="2" size="128"/></heap>
<total type="fast" count="3" size="1048576"/>
<total type="rest" count="455" size="3145728"/>
<total type="mmap" count="2" size="2097152"/>
<system type="current" size="10485760"/>
<system type="max" size="12582912"/>
<aspace type="total" size="10485760"/>
<aspace type="mprotect" size="10485760"/>
</malloc>
"""


def test_malloc_info_is_read_across_every_pool() -> None:
    stats = parse_malloc_info(_MALLOC_INFO)

    assert stats.arenas == 2
    assert stats.heap_bytes == 10485760  # every pool's current size
    assert stats.free_bytes == 1048576 + 3145728  # fast + rest: freed, not returned
    assert stats.mmap_bytes == 2097152  # large blocks straight from the OS, in use
    assert stats.in_use_bytes == 10485760 - (1048576 + 3145728) + 2097152


@needs_glibc
def test_malloc_info_is_read_from_glibc() -> None:
    xml = memory_profile.read_malloc_info()

    assert xml is not None
    stats = parse_malloc_info(xml)
    assert stats.arenas >= 1
    assert stats.heap_bytes > 0


class _FakeFunction:
    def __init__(self, result: int | None) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, *args: object) -> int | None:
        self.calls += 1
        self.args = args
        return self.result


_STREAM = 0x5EED


class _FakeOpenMemstream(_FakeFunction):
    """Like open_memstream: hands the caller a buffer through its ``char **`` and returns a stream."""

    def __init__(self) -> None:
        super().__init__(_STREAM)
        self.buffer_address: int | None = None

    def __call__(self, *args: object) -> int | None:
        buffer = args[0]._obj  # ty: ignore[unresolved-attribute] - the c_char_p behind byref()
        buffer.value = b"<malloc/>"
        self.buffer_address = ctypes.cast(buffer, ctypes.c_void_p).value
        return super().__call__(*args)


class _FailingLibc:
    """open_memstream succeeds, malloc_info fails."""

    def __init__(self) -> None:
        self.open_memstream = _FakeOpenMemstream()
        self.malloc_info = _FakeFunction(1)
        self.fclose = _FakeFunction(0)
        self.free = _FakeFunction(None)


def test_a_failing_malloc_info_still_releases_its_stream_and_buffer() -> None:
    libc = _FailingLibc()

    assert memory_profile.read_malloc_info(libc) is None
    assert libc.fclose.calls == 1
    assert libc.fclose.args == (_STREAM,)
    assert libc.free.calls == 1  # no leak per failed report
    (freed,) = libc.free.args
    assert isinstance(freed, ctypes.c_void_p)
    assert freed.value == libc.open_memstream.buffer_address  # the buffer, not the stream


# ── The cheap report ──


def test_it_logs_nothing_when_off(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler()

    for _ in range(50):
        profiler.task_finished(every=0)

    assert capsys.readouterr().out == ""  # nothing at all, not only no report
    assert not tracemalloc.is_tracing()


def test_it_logs_once_every_n_finished_tasks(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler()

    for _ in range(7):
        profiler.task_finished(every=3)

    assert [report["tasks"] for report in _reports(capsys)] == [3, 6]


@needs_glibc
def test_a_cheap_report_carries_process_and_allocator_memory(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler()

    profiler.task_finished(every=1)

    (report,) = _reports(capsys)
    assert report["rss_mib"] > 0
    assert report["python_blocks"] > 0
    assert report["malloc_arenas"] >= 1
    assert report["malloc_heap_mib"] > 0
    assert report["malloc_in_use_mib"] > 0
    assert report["malloc_free_mib"] >= 0
    assert "top" not in report
    assert not tracemalloc.is_tracing()  # the cheap report never turns tracing on


def test_a_cheap_report_maps_each_figure_to_its_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Exact values from a known malloc_info and /proc status: a swapped or mis-scaled field fails.
    status = tmp_path / "status"
    status.write_text("VmPeak:\t 999999 kB\nVmHWM:\t 888888 kB\nVmRSS:\t 263270 kB\n")
    monkeypatch.setattr(memory_profile, "read_malloc_info", lambda: _MALLOC_INFO)
    read_rss = memory_profile._rss_bytes
    monkeypatch.setattr(memory_profile, "_rss_bytes", lambda: read_rss(str(status)))
    profiler = MemoryProfiler()

    profiler.task_finished(every=1)

    (report,) = _reports(capsys)
    assert report["message"] == "Worker memory profile"
    assert report["level"] == "info"
    assert report["rss_mib"] == 257.1  # VmRSS, kB -> MiB, one decimal
    assert report["malloc_arenas"] == 2
    assert report["malloc_heap_mib"] == 10.0
    assert report["malloc_free_mib"] == 4.0
    assert report["malloc_in_use_mib"] == 8.0  # 10 - 4 + 2 (mmap)


def test_without_glibc_the_allocator_figures_are_null(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(memory_profile, "read_malloc_info", lambda: None)
    profiler = MemoryProfiler()

    profiler.task_finished(every=1)

    (report,) = _reports(capsys)
    assert [report[key] for key in ("malloc_arenas", "malloc_heap_mib", "malloc_in_use_mib", "malloc_free_mib")] == [
        None
    ] * 4


@needs_glibc
def test_memory_held_shows_as_in_use(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler()
    profiler.task_finished(every=1)

    _held.extend(bytes(40_000) for _ in range(1024))  # ~39 MiB, held, below the mmap threshold
    profiler.task_finished(every=1)

    before, after = _reports(capsys)
    assert after["malloc_in_use_mib"] - before["malloc_in_use_mib"] >= 35


def test_small_python_objects_show_as_python_blocks(capsys: pytest.CaptureFixture[str]) -> None:
    # Python keeps small objects in its own pools, mapped from the OS: malloc_info never sees them.
    profiler = MemoryProfiler()
    profiler.task_finished(every=1)

    _held.extend(str(i) * 3 for i in range(1_000_000))  # ~1M small objects, held
    profiler.task_finished(every=1)

    before, after = _reports(capsys)
    assert after["python_blocks"] - before["python_blocks"] >= 900_000


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


def test_a_forked_child_starts_its_own_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The CPU worker's prefork children inherit the parent's state; each counts on its own.
    profiler = MemoryProfiler()
    profiler.task_finished(every=2)

    monkeypatch.setattr(memory_profile.os, "getpid", lambda: -1)
    for _ in range(3):
        profiler.task_finished(every=2)

    # Counting on from the parent's one task would report at 2 and again at 4.
    assert [(report["pid"], report["tasks"]) for report in _reports(capsys)] == [(-1, 2)]


def test_a_forked_child_takes_its_own_baseline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A child comparing against the parent's snapshot would report the parent's later memory as growth.
    profiler = MemoryProfiler()
    profiler.task_finished(every=1, trace_frames=2)  # the parent's baseline
    _held.extend(bytes(1024) for _ in range(4096))  # ~4 MiB more, before the fork
    capsys.readouterr()

    monkeypatch.setattr(memory_profile.os, "getpid", lambda: -1)
    profiler.task_finished(every=1, trace_frames=2)  # the child's first report: its own baseline

    (report,) = _reports(capsys)
    assert [site for site in report["top"] if site["growth_kib"] >= 1024] == []


# ── The heavy mode: tracemalloc, on only when asked ──


def test_the_heavy_mode_names_where_memory_grew(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler(top=5)
    profiler.task_finished(every=1, trace_frames=5)  # the baseline
    capsys.readouterr()

    line = sys._getframe().f_lineno + 1
    _held.extend(bytes(1024) for _ in range(4096))  # ~4 MiB, held
    profiler.task_finished(every=1, trace_frames=5)

    (report,) = _reports(capsys)
    assert len(report["top"]) == 5
    grown = report["top"][0]  # the largest growth is listed first
    assert grown["frames"][0] == f"{__file__}:{line}"  # innermost frame: the allocating line
    assert grown["growth_kib"] >= 4096
    assert grown["size_kib"] >= grown["growth_kib"]
    assert grown["size_kib"] < 8192  # KiB, not bytes
    assert grown["count"] >= 4096


def test_the_heavy_mode_lists_growth_not_memory_released_since(capsys: pytest.CaptureFixture[str]) -> None:
    tracemalloc.start(5)  # as on a worker already tracing: what is alive now is in the baseline
    released = [bytes(1024) for _ in range(20_480)]  # ~20 MiB
    profiler = MemoryProfiler(top=1)
    profiler.task_finished(every=1, trace_frames=5)
    capsys.readouterr()

    released.clear()  # a large release, which compare_to would rank first
    _held.extend(bytes(1024) for _ in range(1024))  # a small leak, ~1 MiB
    profiler.task_finished(every=1, trace_frames=5)

    (report,) = _reports(capsys)
    (grown,) = report["top"]
    assert grown["growth_kib"] >= 1024


def test_the_heavy_mode_keeps_the_frames_it_was_asked_for(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler(top=1)
    profiler.task_finished(every=1, trace_frames=3)
    capsys.readouterr()

    def inner() -> None:
        _held.extend(bytes(1024) for _ in range(4096))

    def outer() -> None:
        inner()

    outer()
    profiler.task_finished(every=1, trace_frames=3)

    (report,) = _reports(capsys)
    frames = report["top"][0]["frames"]
    assert len(frames) == 3  # the caller chain, not only the line that allocated
    assert sum("test_memory_profile.py" in frame for frame in frames) >= 2


def test_the_heavy_mode_separates_size_growth_and_count(capsys: pytest.CaptureFixture[str]) -> None:
    # One site that already held memory at the baseline and then grew: size and growth differ.
    tracemalloc.start(5)

    def grow(kib: int) -> None:
        _held.extend(bytes(1024) for _ in range(kib))

    profiler = MemoryProfiler(top=1)
    for kib in (2048, 4096):  # the first report is the baseline, holding 2 MiB from this site
        capsys.readouterr()
        grow(kib)  # one call site, so one traceback
        profiler.task_finished(every=1, trace_frames=5)

    (report,) = _reports(capsys)
    (site,) = report["top"]
    assert 4096 <= site["growth_kib"] < 5000  # what was added since the baseline
    assert site["size_kib"] >= site["growth_kib"] + 1900  # all it holds now, the baseline's 2 MiB too
    assert site["count"] >= 6000  # every block it holds, not only the new ones


def test_the_heavy_mode_restarts_tracing_at_the_depth_asked_for(capsys: pytest.CaptureFixture[str]) -> None:
    # Tracing already on at another depth (PYTHONTRACEMALLOC=1, say) is restarted, not reused.
    tracemalloc.start(1)
    profiler = MemoryProfiler(top=1)
    profiler.task_finished(every=1, trace_frames=3)
    capsys.readouterr()

    def inner() -> None:
        _held.extend(bytes(1024) for _ in range(4096))

    inner()
    profiler.task_finished(every=1, trace_frames=3)

    (report,) = _reports(capsys)
    assert len(report["top"][0]["frames"]) == 3


def test_the_heavy_mode_works_at_a_depth_of_one(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler(top=1)
    profiler.task_finished(every=1, trace_frames=1)

    (report,) = _reports(capsys)
    assert "top" in report
    assert tracemalloc.get_traceback_limit() == 1


def test_the_heavy_mode_does_not_report_its_own_bookkeeping(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler(top=50)
    profiler.task_finished(every=1, trace_frames=5)
    capsys.readouterr()

    profiler.task_finished(every=1, trace_frames=5)  # nothing allocated in between

    (report,) = _reports(capsys)
    own = ("memory_profile.py", "tracemalloc.py", "fnmatch.py", "_compiler.py", "_parser.py")
    assert [stat for stat in report["top"] if any(name in stat["frames"][0] for name in own)] == []


# ── Wiring: the worker's own profiler, fed by Celery's task_postrun signal ──


def test_the_settings_are_read_from_their_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_MEMORY_PROFILE_EVERY", "50")
    monkeypatch.setenv("CELERY_MEMORY_PROFILE_TRACE_FRAMES", "10")

    fresh = Settings()
    assert fresh.celery_memory_profile_every == 50
    assert fresh.celery_memory_profile_trace_frames == 10


def test_both_are_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELERY_MEMORY_PROFILE_EVERY", raising=False)
    monkeypatch.delenv("CELERY_MEMORY_PROFILE_TRACE_FRAMES", raising=False)

    fresh = Settings()
    assert fresh.celery_memory_profile_every == 0
    assert fresh.celery_memory_profile_trace_frames == 0


def test_every_finished_task_reaches_the_worker_profiler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(core, "_memory_profiler", MemoryProfiler())
    monkeypatch.setattr(settings, "celery_memory_profile_every", 2)
    monkeypatch.setattr(settings, "celery_memory_profile_trace_frames", 0)

    for _ in range(4):
        signals.task_postrun.send(sender=None)

    reports = _reports(capsys)
    assert [report["tasks"] for report in reports] == [2, 4]
    assert all("top" not in report for report in reports)


def test_the_trace_setting_reaches_the_worker_profiler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(core, "_memory_profiler", MemoryProfiler())
    monkeypatch.setattr(settings, "celery_memory_profile_every", 1)
    monkeypatch.setattr(settings, "celery_memory_profile_trace_frames", 4)

    signals.task_postrun.send(sender=None)

    (report,) = _reports(capsys)
    assert "top" in report
    assert tracemalloc.get_traceback_limit() == 4


def test_the_worker_profiler_is_silent_while_the_setting_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(core, "_memory_profiler", MemoryProfiler())
    monkeypatch.setattr(settings, "celery_memory_profile_every", 0)
    monkeypatch.setattr(settings, "celery_memory_profile_trace_frames", 4)

    for _ in range(4):
        signals.task_postrun.send(sender=None)

    assert capsys.readouterr().out == ""
