"""Pure settle-decision helper for the workout.created HR-zone debounce.

A ``workout.created`` webhook lands the instant Apple reports the workout, but
the heart-rate trace uploads in later chunks — so zones computed at ingest are
partial. The Celery debounce polls completeness over a few ticks and asks this
helper whether the trace has settled enough to emit.

Pure by design: no I/O, no DB, no settings/datetime imports. Callers pass in the
elapsed time, target completeness and hard cap.
"""

from __future__ import annotations


def should_emit(
    prev_completeness: float | None,
    completeness: float | None,
    elapsed_seconds: float,
    target: float,
    cap_seconds: float,
) -> bool:
    """Return True iff the HR trace has settled enough to emit now.

    Emit iff ANY of:
      - ``completeness is None`` — no HR series at all → nothing to wait for.
      - ``completeness >= target`` — good enough → emit now.
      - ``elapsed_seconds >= cap_seconds`` — hard cap → emit whatever we have.
      - ``prev_completeness is not None`` AND ``completeness == prev_completeness``
        AND ``completeness > 0`` — HR stopped growing tick-over-tick → settled.

    Otherwise the trace is still growing under both target and cap → wait.
    """
    if completeness is None:
        return True
    if completeness >= target:
        return True
    if elapsed_seconds >= cap_seconds:
        return True
    # HR stopped growing tick-over-tick (and is non-zero) → settled.
    return prev_completeness is not None and completeness == prev_completeness and completeness > 0
