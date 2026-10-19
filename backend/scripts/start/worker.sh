#!/bin/bash
set -e -x

# Fork delta: CELERY_QUEUES makes this container a dedicated worker for exactly those
# queues (the fast-lane worker runs with CELERY_QUEUES=webhook_sync). It then starts no
# CPU worker, so a dedicated container never picks up xml_sync work. See FORK-DELTA.md.
if [ -n "${CELERY_QUEUES:-}" ]; then
    echo "Starting dedicated worker for queues: ${CELERY_QUEUES}"
    exec uv run celery -A app.main:celery_app worker --loglevel=info --pool=threads -Q "${CELERY_QUEUES}" -n io@%h
fi

# Fork delta: the container stops when EITHER worker exits, so the platform restarts it.
# Upstream ran the CPU worker in the foreground, which kept the container alive after the
# I/O worker died: on 2026-10-16 nothing consumed sdk_sync or default for 2.7 days. Both
# now run in the background; SIGTERM is passed on to both. See FORK-DELTA.md.
#
# Stopping is bounded: TERM is Celery's warm shutdown, which waits for running tasks (an
# xml_sync task has no time limit), so a worker still running WORKER_STOP_GRACE_SECONDS
# after TERM is killed. The grace is whole seconds; anything else falls back to 30, because a
# value `sleep` rejects would otherwise cancel the kill.
stop_grace="${WORKER_STOP_GRACE_SECONDS:-30}"
case "$stop_grace" in
    *[!0-9]*)
        echo "WORKER_STOP_GRACE_SECONDS='${stop_grace}' is not a whole number of seconds; using 30" >&2
        stop_grace=30
        ;;
esac
stop_workers() {
    kill -TERM "$IO_PID" "$CPU_PID" 2>/dev/null || true
    (
        trap 'kill "$sleeper" 2>/dev/null; exit 0' TERM
        sleep "$stop_grace" &
        sleeper=$!
        # A failed sleep still kills: fail toward stopping, never toward waiting for ever.
        wait "$sleeper" || true
        kill -KILL "$IO_PID" "$CPU_PID" 2>/dev/null || true
    ) &
    local timer=$!
    wait "$IO_PID" "$CPU_PID" 2>/dev/null || true
    kill -TERM "$timer" 2>/dev/null || true
    wait "$timer" 2>/dev/null || true
}
trap 'stop_workers; exit 143' TERM INT

echo "Starting I/O worker..."
uv run celery -A app.main:celery_app worker --loglevel=info --pool=threads -Q default,sdk_sync,garmin_sync,webhook_sync -n io@%h &
IO_PID=$!

echo "Starting CPU worker..."
uv run celery -A app.main:celery_app worker --loglevel=info --pool=prefork --concurrency=2 -Q xml_sync -n cpu@%h &
CPU_PID=$!

status=0
wait -n || status=$?
echo "A Celery worker exited (status ${status}); stopping the container so it is restarted" >&2
stop_workers
exit 1
