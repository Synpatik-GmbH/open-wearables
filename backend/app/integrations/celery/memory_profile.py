"""Memory profile of a Celery worker process (fork-only diagnostic).

With ``CELERY_MEMORY_PROFILE_EVERY=N`` (N > 0) every worker process logs one
``worker_memory_profile`` line per N finished tasks. The line is cheap (no tracing, well under a
millisecond) and says which of three causes is growing a worker's memory:

- ``malloc_free_mib`` grows: glibc keeps freed memory in its pools rather than returning it, the
  per-thread pools of ``--pool=threads`` in particular. ``MALLOC_ARENA_MAX`` is the fix.
- ``malloc_in_use_mib`` grows: something holds memory taken through glibc: large Python objects,
  or a C library (database driver, TLS, XML).
- ``python_blocks`` grows: small Python objects are held. Python keeps them in its own pools, which
  glibc does not see; the count stands in for their size, as each is at most 512 bytes.

Read trends, not single lines. A report is taken as a task finishes, so it includes that task's
arguments and result, and in the threads pool every task still running: one line can spike for
reasons that are gone a moment later. A leak raises the floor, so compare per-process minimums over
an hour or more.

To find what holds it, ``CELERY_MEMORY_PROFILE_TRACE_FRAMES=K`` (K > 0) also starts ``tracemalloc``
with K frames per allocation, and each line then carries ``top``: the allocation sites whose memory
grew most since the process's first report, innermost frame first. Memory that grows with no Python site
among them is held by a C library. This mode is heavy: every report holds the GIL for seconds on a large process and
the snapshots cost memory in proportion to live allocations, so run it briefly, on dev. See
FORK-DELTA.md.
"""

import ctypes
import ctypes.util
import os
import sys
import threading
import tracemalloc
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

_MIB = 1024 * 1024

# The heavy mode's own bookkeeping, kept out of the growth it reports.
_IGNORED_FRAMES = (
    tracemalloc.Filter(False, tracemalloc.__file__),
    tracemalloc.Filter(False, __file__),
)


@dataclass(frozen=True)
class MallocStats:
    """glibc's totals over every pool (arena), from ``malloc_info``."""

    arenas: int
    heap_bytes: int  # the pools' current size: address space, not only resident pages
    free_bytes: int  # freed chunks the pools keep (fast + rest)
    mmap_bytes: int  # large blocks taken straight from the OS; in use until freed

    @property
    def in_use_bytes(self) -> int:
        return self.heap_bytes - self.free_bytes + self.mmap_bytes


def parse_malloc_info(xml: str) -> MallocStats:
    root = ElementTree.fromstring(xml)
    totals = {element.get("type"): int(element.get("size", "0")) for element in root.findall("total")}
    system = {element.get("type"): int(element.get("size", "0")) for element in root.findall("system")}
    return MallocStats(
        arenas=len(root.findall("heap")),
        heap_bytes=system.get("current", 0),
        free_bytes=totals.get("fast", 0) + totals.get("rest", 0),
        mmap_bytes=totals.get("mmap", 0),
    )


def read_malloc_info() -> str | None:
    """glibc's ``malloc_info`` XML for this process, or None where there is no glibc."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        malloc_info = libc.malloc_info
        open_memstream = libc.open_memstream
    except (OSError, AttributeError, TypeError):
        return None
    open_memstream.restype = ctypes.c_void_p
    open_memstream.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_size_t)]
    malloc_info.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.fclose.argtypes = [ctypes.c_void_p]
    libc.free.argtypes = [ctypes.c_void_p]
    buffer = ctypes.c_char_p()
    size = ctypes.c_size_t()
    stream = open_memstream(ctypes.byref(buffer), ctypes.byref(size))
    if not stream:
        return None
    try:
        if malloc_info(0, stream) != 0:
            return None
    finally:
        libc.fclose(stream)
    try:
        return ctypes.string_at(buffer, size.value).decode()
    finally:
        libc.free(ctypes.cast(buffer, ctypes.c_void_p))


def _rss_bytes(status_path: str = "/proc/self/status") -> int | None:
    """Resident set size from /proc (Linux), or None where there is no /proc."""
    try:
        with open(status_path) as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _mib(value: int | None) -> float | None:
    return round(value / _MIB, 1) if value is not None else None


class MemoryProfiler:
    """Counts finished tasks in this process and logs a memory report every ``every`` of them."""

    def __init__(self, top: int = 15) -> None:
        self._top = top
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._count = 0
        self._baseline: tracemalloc.Snapshot | None = None

    def task_finished(self, every: int, trace_frames: int = 0) -> None:
        if every <= 0:
            return
        with self._lock:
            pid = os.getpid()
            if pid != self._pid:
                # First task in this process, or a prefork child that inherited the parent's state.
                self._pid = pid
                self._count = 0
                self._baseline = None
            self._count += 1
            if self._count % every:
                return
            report = self._cheap_report(pid)
            if trace_frames > 0:
                report["top"] = self._growth(trace_frames)
        log_structured(logger, "info", "Worker memory profile", action="worker_memory_profile", **report)

    def _cheap_report(self, pid: int) -> dict[str, Any]:
        xml = read_malloc_info()
        malloc = parse_malloc_info(xml) if xml is not None else None
        return {
            "pid": pid,
            "tasks": self._count,
            "rss_mib": _mib(_rss_bytes()),
            "python_blocks": sys.getallocatedblocks(),
            "malloc_arenas": malloc.arenas if malloc else None,
            "malloc_heap_mib": _mib(malloc.heap_bytes) if malloc else None,
            "malloc_in_use_mib": _mib(malloc.in_use_bytes) if malloc else None,
            "malloc_free_mib": _mib(malloc.free_bytes) if malloc else None,
        }

    def _growth(self, frames: int) -> list[dict[str, Any]]:
        if not tracemalloc.is_tracing() or tracemalloc.get_traceback_limit() != frames:
            tracemalloc.stop()
            tracemalloc.start(frames)
            self._baseline = None
        if self._baseline is None:
            self._baseline = tracemalloc.take_snapshot().filter_traces(_IGNORED_FRAMES)
        snapshot = tracemalloc.take_snapshot().filter_traces(_IGNORED_FRAMES)
        # compare_to ranks by the size of the change either way; memory released since the baseline
        # must not crowd out the growth being looked for.
        grown = [stat for stat in snapshot.compare_to(self._baseline, "traceback") if stat.size_diff > 0]
        return [
            {
                "frames": [f"{frame.filename}:{frame.lineno}" for frame in reversed(stat.traceback)],
                "size_kib": round(stat.size / 1024),
                "growth_kib": round(stat.size_diff / 1024),
                "count": stat.count,
            }
            for stat in grown[: self._top]
        ]
