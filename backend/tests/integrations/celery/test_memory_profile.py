"""The memory profile of a Celery worker process.

Prod's I/O worker grew from ~650 to ~880 MiB between 2026-09-26 and 2026-10-16 and then died; dev's
grew from ~535 to ~800 MiB after the 0.9.0 move on 2026-10-05 (Azure Monitor ``WorkingSetBytes``,
read 2026-10-19). The cheap report says which cause it is: the allocator keeping freed memory
(``malloc_free_mib`` grows), or something holding memory (``malloc_in_use_mib`` or ``python_blocks``
grows).
"""

import ctypes
import ctypes.util
import json
import platform
import threading
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
def _release_held() -> Iterator[None]:
    yield
    _held.clear()


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
    def __init__(self, result: int | None, calls_log: list[str], name: str, raises: Exception | None = None) -> None:
        self.result = result
        self.calls_log = calls_log
        self.name = name
        self.raises = raises

    def __call__(self, *args: object) -> int | None:
        self.args = args
        self.calls_log.append(self.name)
        if self.raises is not None:
            raise self.raises
        return self.result


_STREAM = 0x5EED
_XML = b"<malloc/>"


class _FakeLibc:
    """Like glibc's memstream: the buffer and its size reach the caller only when fclose flushes them.

    malloc_info returns the given status, or raises when ``raises`` is set.
    """

    def __init__(self, status: int, raises: bool = False) -> None:
        self.order: list[str] = []
        self.buffer_address: int | None = None
        self._out: tuple[ctypes.c_char_p, ctypes.c_size_t] | None = None
        refused = ctypes.ArgumentError("malloc_info refused its arguments") if raises else None
        self.malloc_info = _FakeFunction(status, self.order, "malloc_info", raises=refused)
        self.free = _FakeFunction(None, self.order, "free")

    def open_memstream(self, buffer_ref: object, size_ref: object) -> int:
        self.order.append("open_memstream")
        self._out = (buffer_ref._obj, size_ref._obj)  # ty: ignore[unresolved-attribute] - behind byref()
        return _STREAM

    def fclose(self, stream: int) -> int:
        self.order.append("fclose")
        assert stream == _STREAM
        assert self._out is not None
        buffer, size = self._out
        buffer.value = _XML  # published on close, as glibc does
        size.value = len(_XML)
        self.buffer_address = ctypes.cast(buffer, ctypes.c_void_p).value
        return 0


def _assert_released(libc: _FakeLibc) -> None:
    # fclose before free: free before the flush would release the initial null and leak the buffer.
    assert libc.order == ["open_memstream", "malloc_info", "fclose", "free"]
    (freed,) = libc.free.args
    assert isinstance(freed, ctypes.c_void_p)
    assert freed.value is not None
    assert freed.value == libc.buffer_address  # the buffer, not the stream


@pytest.mark.parametrize(("status", "expected"), [(0, _XML.decode()), (1, None)])
def test_malloc_info_releases_its_stream_and_buffer_either_way(status: int, expected: str | None) -> None:
    libc = _FakeLibc(status)

    assert memory_profile.read_malloc_info(libc) == expected
    _assert_released(libc)


def test_malloc_info_raising_still_releases_its_stream_and_buffer() -> None:
    libc = _FakeLibc(0, raises=True)

    with pytest.raises(ctypes.ArgumentError):
        memory_profile.read_malloc_info(libc)
    _assert_released(libc)


@needs_glibc
def test_the_c_library_is_looked_up_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # find_library runs ldconfig in a subprocess; once per process, not once per report.
    lookups: list[str] = []
    find_library = ctypes.util.find_library
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: lookups.append(name) or find_library(name))
    memory_profile._glibc.cache_clear()
    try:
        for _ in range(5):
            assert memory_profile.read_malloc_info() is not None
    finally:
        memory_profile._glibc.cache_clear()

    assert lookups == ["c"]


# ── The cheap report ──


def test_it_logs_nothing_when_off(capsys: pytest.CaptureFixture[str]) -> None:
    profiler = MemoryProfiler()

    for _ in range(50):
        profiler.task_finished(every=0)

    assert capsys.readouterr().out == ""  # nothing at all, not only no report


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


# ── Wiring: the worker's own profiler, fed by Celery's task_postrun signal ──


def test_the_setting_is_read_from_its_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_MEMORY_PROFILE_EVERY", "50")

    assert Settings().celery_memory_profile_every == 50


def test_it_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELERY_MEMORY_PROFILE_EVERY", raising=False)

    assert Settings().celery_memory_profile_every == 0


def test_every_finished_task_reaches_the_worker_profiler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(core, "_memory_profiler", MemoryProfiler())
    monkeypatch.setattr(settings, "celery_memory_profile_every", 2)

    for _ in range(4):
        signals.task_postrun.send(sender=None)

    assert [report["tasks"] for report in _reports(capsys)] == [2, 4]


def test_the_worker_profiler_is_silent_while_the_setting_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(core, "_memory_profiler", MemoryProfiler())
    monkeypatch.setattr(settings, "celery_memory_profile_every", 0)

    for _ in range(4):
        signals.task_postrun.send(sender=None)

    assert capsys.readouterr().out == ""
