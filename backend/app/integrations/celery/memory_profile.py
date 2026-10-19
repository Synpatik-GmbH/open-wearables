"""Memory profile of a Celery worker process (fork-only diagnostic).

With ``CELERY_MEMORY_PROFILE_EVERY=N`` (N > 0) every worker process logs one
``worker_memory_profile`` line per N finished tasks. The line is cheap (no tracing, well under a
millisecond) and says which cause is growing a worker's memory:

- ``malloc_free_mib`` grows: glibc keeps freed memory in its pools rather than returning it, the
  per-thread pools of ``--pool=threads`` in particular. ``MALLOC_ARENA_MAX`` is the fix.
- ``malloc_in_use_mib`` grows: something holds memory taken through glibc: large Python objects,
  or a C library (database driver, TLS, XML).
- ``python_blocks`` grows: Python allocations are held. It counts allocations of every size, not
  bytes; the small ones live in Python's own pools, which glibc does not see, so for them it is the
  only figure that moves.

Read trends, not single lines. A report is taken as a task finishes, so it includes that task's
arguments and result, and in the threads pool every task still running: one line can spike for
reasons that are gone a moment later. A leak raises the floor, so compare per-process minimums over
an hour or more. See FORK-DELTA.md.
"""

import ctypes
import ctypes.util
import functools
import os
import sys
import threading
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

_MIB = 1024 * 1024


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


@functools.cache
def _glibc() -> Any:
    """This process's C library, loaded once (find_library runs ldconfig), or None without glibc."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        libc.malloc_info, libc.open_memstream  # noqa: B018 - absent outside glibc
    except (OSError, AttributeError, TypeError):
        return None
    libc.open_memstream.restype = ctypes.c_void_p
    libc.open_memstream.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_size_t)]
    libc.malloc_info.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.fclose.argtypes = [ctypes.c_void_p]
    libc.free.argtypes = [ctypes.c_void_p]
    return libc


def read_malloc_info(libc: Any = None) -> str | None:
    """glibc's ``malloc_info`` XML for this process, or None where there is no glibc."""
    libc = libc if libc is not None else _glibc()
    if libc is None:
        return None
    buffer = ctypes.c_char_p()
    size = ctypes.c_size_t()
    stream = libc.open_memstream(ctypes.byref(buffer), ctypes.byref(size))
    if not stream:
        return None
    try:
        try:
            status = libc.malloc_info(0, stream)
        finally:
            libc.fclose(stream)  # flushes into buffer, which is ours to free whatever malloc_info did
        return ctypes.string_at(buffer, size.value).decode() if status == 0 else None
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

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._count = 0

    def task_finished(self, every: int) -> None:
        if every <= 0:
            return
        with self._lock:
            pid = os.getpid()
            if pid != self._pid:
                # First task in this process, or a prefork child that inherited the parent's state.
                self._pid = pid
                self._count = 0
            self._count += 1
            if self._count % every:
                return
            report = self._report(pid)
        log_structured(logger, "info", "Worker memory profile", action="worker_memory_profile", **report)

    def _report(self, pid: int) -> dict[str, Any]:
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
