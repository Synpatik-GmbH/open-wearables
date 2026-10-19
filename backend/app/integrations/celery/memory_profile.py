"""Opt-in memory profile of a Celery worker process (fork-only diagnostic).

With ``CELERY_MEMORY_PROFILE_EVERY=N`` (N > 0) every worker process logs one
``worker_memory_profile`` line per N finished tasks: its RSS, the memory Python's allocator
tracks (``tracemalloc``), and the source lines whose traced memory grew most since the process's
first report. Traced memory growing with RSS is a Python leak, and ``top`` names where; RSS growing
while traced memory stays flat is the C allocator keeping freed pages.

Off by default: ``tracemalloc`` slows every allocation and holds one snapshot per process. See
FORK-DELTA.md.
"""

import os
import threading
import tracemalloc
from logging import getLogger
from typing import Any

from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

_MIB = 1024 * 1024

# The profiler's own bookkeeping, kept out of the growth it reports.
_IGNORED_FRAMES = (
    tracemalloc.Filter(False, tracemalloc.__file__),
    tracemalloc.Filter(False, __file__),
    tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
    tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
)


def _rss_bytes() -> int | None:
    """Resident set size from /proc (Linux), or None where there is no /proc."""
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


class MemoryProfiler:
    """Counts finished tasks in this process and logs a memory report every ``every`` of them."""

    def __init__(self, top: int = 15) -> None:
        self._top = top
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._count = 0
        self._baseline: tracemalloc.Snapshot | None = None

    def task_finished(self, every: int) -> None:
        if every <= 0:
            return
        with self._lock:
            pid = os.getpid()
            if pid != self._pid:
                # First task in this process, or a prefork child that inherited the parent's state.
                self._pid = pid
                self._count = 0
                self._baseline = None
                if not tracemalloc.is_tracing():
                    tracemalloc.start()
            self._count += 1
            if self._count % every:
                return
            report = self._report(pid)
        log_structured(logger, "info", "Worker memory profile", action="worker_memory_profile", **report)

    def _report(self, pid: int) -> dict[str, Any]:
        snapshot = tracemalloc.take_snapshot().filter_traces(_IGNORED_FRAMES)
        if self._baseline is None:
            self._baseline = snapshot
        traced, peak = tracemalloc.get_traced_memory()
        rss = _rss_bytes()
        growth = snapshot.compare_to(self._baseline, "lineno")
        return {
            "pid": pid,
            "tasks": self._count,
            "rss_mib": round(rss / _MIB, 1) if rss is not None else None,
            "traced_mib": round(traced / _MIB, 1),
            "traced_peak_mib": round(peak / _MIB, 1),
            "top": [
                {
                    "where": f"{stat.traceback[0].filename}:{stat.traceback[0].lineno}",
                    "size_kib": round(stat.size / 1024),
                    "growth_kib": round(stat.size_diff / 1024),
                    "count": stat.count,
                }
                for stat in growth[: self._top]
            ],
        }
