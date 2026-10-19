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
stop_workers() {
    kill -TERM "$IO_PID" "$CPU_PID" 2>/dev/null || true
    wait || true
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
