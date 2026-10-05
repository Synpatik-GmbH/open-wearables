#!/bin/bash
set -e -x

# Fork delta: CELERY_QUEUES makes this container a dedicated worker for exactly those
# queues (the fast-lane worker runs with CELERY_QUEUES=webhook_sync). It then starts no
# CPU worker, so a dedicated container never picks up xml_sync work. See FORK-DELTA.md.
if [ -n "${CELERY_QUEUES:-}" ]; then
    echo "Starting dedicated worker for queues: ${CELERY_QUEUES}"
    exec uv run celery -A app.main:celery_app worker --loglevel=info --pool=threads -Q "${CELERY_QUEUES}" -n io@%h
fi

echo "Starting I/O worker..."
uv run celery -A app.main:celery_app worker --loglevel=info --pool=threads -Q default,sdk_sync,garmin_sync,webhook_sync -n io@%h &

echo "Starting CPU worker..."
uv run celery -A app.main:celery_app worker --loglevel=info --pool=prefork --concurrency=2 -Q xml_sync -n cpu@%h
