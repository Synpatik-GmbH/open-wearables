"""Celery task that emits outgoing webhook events via Svix.

Called asynchronously after data is saved to the database so the
request / ingestion path is never blocked by webhook delivery.
"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from celery import shared_task

from app.database import SessionLocal
from app.services import developer_service
from app.services.outgoing_webhooks import svix as svix_service

logger = getLogger(__name__)


@shared_task(
    name="app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event",
    bind=True,
    max_retries=2,
    default_retry_delay=5,
    acks_late=True,
)
def emit_webhook_event(
    self: Any,
    event_type: str,
    payload: dict[str, Any],
    *,
    channels: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Send a webhook event to every developer that has a registered endpoint.

    Developers without an endpoint are skipped: Svix would store the payload
    for an application with nowhere to deliver it, so each such account added
    another stored copy of every event.  Skipping them also means no Svix
    application is created for an account that never asked for webhooks.
    """
    if not svix_service.is_enabled():
        logger.debug("Svix is not configured — skipping webhook dispatch for event %s", event_type)
        return {"event_type": event_type, "sent": 0, "skipped": 0, "errors": []}

    with SessionLocal() as db:
        page_size = 100
        offset = 0
        developers = []
        while True:
            batch = developer_service.crud.get_all(db, filters={}, offset=offset, limit=page_size, sort_by=None)
            developers.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

    sent = 0
    skipped = 0
    errors: list[str] = []
    for dev in developers:
        app_id = str(dev.id)
        # The application is created when the developer registers an endpoint,
        # so having an endpoint implies the application already exists.
        if not svix_service.has_endpoints(app_id):
            skipped += 1
            continue
        result = svix_service.send(
            event_type,
            app_id,
            payload,
            channels=channels,
            idempotency_key=idempotency_key,
        )
        if result is not None:
            sent += 1
        else:
            errors.append(app_id)

    if errors:
        exc = RuntimeError(
            f"Webhook delivery failed for {len(errors)} of {sent + len(errors)} developer(s) "
            f"[event={event_type}, failed={errors}]"
        )
        logger.error(
            "Webhook delivery failed for %d developer(s) on event %s; scheduling retry",
            len(errors),
            event_type,
        )
        raise self.retry(exc=exc)

    return {"event_type": event_type, "sent": sent, "skipped": skipped, "errors": errors}
