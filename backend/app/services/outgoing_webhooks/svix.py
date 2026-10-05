"""Thin wrapper around the Svix Python SDK.

Responsibilities:
- Initialise Svix client from config
- Lazy Application creation per developer
- Register / sync event types on startup
- Send (emit) webhook messages
- CRUD proxy for endpoint management
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from jose import jwt
from svix.api import (
    ApplicationIn,
    ApplicationListOptions,
    EndpointIn,
    EndpointListOptions,
    EndpointOut,
    EndpointPatch,
    EventTypeIn,
    EventTypeListOptions,
    EventTypePatch,
    ListResponseEndpointOut,
    ListResponseMessageAttemptOut,
    ListResponseMessageOut,
    MessageAttemptListByEndpointOptions,
    MessageIn,
    MessageListOptions,
    MessageOut,
    Svix,
    SvixOptions,
)
from svix.api.errors.http_error import HttpError

from app.config import settings
from app.constants.webhooks.test_payloads import get_test_payload
from app.schemas.webhooks.event_types import EVENT_TYPE_DESCRIPTIONS, WebhookEventType
from app.services.outgoing_webhooks import pseudonyms
from app.utils.sentry_helpers import log_and_capture_error

logger = logging.getLogger(__name__)

# Fixed org UID used for this self-hosted instance.
_SVIX_ORG_ID = "org_openwearables"

# Svix channels scope messages and endpoint subscriptions per user.  Each emitted message is
# tagged with the user's pseudonymous channel (pseudonyms.user_channel), never a readable id.
# An endpoint without a channel filter receives ALL messages (all users).
# An endpoint with the user's channel receives only messages for that user.
# Svix allows up to 5 channels per message; we always send exactly one.


def user_id_from_endpoint(ep: EndpointOut) -> UUID | None:
    """Extract the user_id filter from an endpoint's Svix channels, if any."""
    if not ep.channels:
        return None
    for ch in ep.channels:
        user_id = pseudonyms.user_id_from_channel(ch)
        if user_id is not None:
            return user_id
    return None


def _resolve_auth_token() -> str | None:
    """Return the Svix auth token, generating it from the JWT secret if needed.

    Priority:
    1. Explicit ``SVIX_AUTH_TOKEN`` env var — use as-is.
    2. ``SVIX_JWT_SECRET`` present — derive a token automatically (no manual step needed).
    3. Neither set — webhooks disabled.
    """
    if settings.svix_auth_token is not None:
        return settings.svix_auth_token.get_secret_value()
    if settings.svix_jwt_secret is not None:
        token = jwt.encode(
            {"sub": _SVIX_ORG_ID},
            settings.svix_jwt_secret.get_secret_value(),
            algorithm="HS256",
        )
        logger.info("SVIX_AUTH_TOKEN not set — derived from SVIX_JWT_SECRET automatically.")
        return token
    logger.warning("Neither SVIX_AUTH_TOKEN nor SVIX_JWT_SECRET is set — outgoing webhooks are disabled.")
    return None


def _build_client() -> Svix | None:
    """Create the Svix client. Returns None when webhooks are disabled or no credentials are configured."""
    if not settings.outgoing_webhooks_enabled:
        return None
    token = _resolve_auth_token()
    if token is None:
        return None
    return Svix(token, SvixOptions(server_url=settings.svix_server_url))


_client: Svix | None = _build_client()


def is_enabled() -> bool:
    return _client is not None


def register_event_types() -> bool:
    """Sync every WebhookEventType into Svix: create the missing, update the changed (idempotent).

    Returns:
        True if the sync completed with no failures (or nothing needed to run). False if
        Svix was unreachable or any individual event type failed — callers must not report
        success in that case, since the sync simply retries next boot.
    """
    if not is_enabled():
        return True
    assert _client is not None

    # List existing types once (paginated) so a steady-state startup makes a single
    # call instead of a create+update round-trip per type. If Svix is still coming up,
    # skip without crashing app startup — the sync is idempotent and reruns next boot.
    existing: dict[str, str] = {}
    iterator: str | None = None
    try:
        while True:
            page = _client.event_type.list(EventTypeListOptions(limit=250, iterator=iterator))
            existing.update({et.name: et.description for et in page.data})
            if page.done:
                break
            iterator = page.iterator
    except Exception:
        logger.warning("Svix unreachable during startup — skipping event-type sync (reruns next boot).")
        return False

    all_ok = True
    for evt in WebhookEventType:
        description = EVENT_TYPE_DESCRIPTIONS.get(evt, "")
        current = existing.get(evt.value)
        if current is None:
            try:
                _client.event_type.create(EventTypeIn(name=evt.value, description=description))
                logger.info("Registered event type: %s", evt.value)
            except Exception:
                # Deemed missing but create failed (archived/race) — update so it is never left unregistered.
                try:
                    _client.event_type.patch(evt.value, EventTypePatch(description=description))
                except Exception:
                    logger.exception("Failed to register/update event type %s", evt.value)
                    all_ok = False
        elif current != description:
            try:
                _client.event_type.patch(evt.value, EventTypePatch(description=description))
                logger.info("Updated event type description: %s", evt.value)
            except Exception:
                logger.exception("Failed to update event type %s", evt.value)
                all_ok = False

    return all_ok


@dataclass(frozen=True)
class LegacyChannelMigration:
    """What one run of :func:`migrate_legacy_user_channels` saw and did."""

    applications: int
    endpoints: int
    legacy: int
    migrated: int


def migrate_legacy_user_channels(*, dry_run: bool = False) -> LegacyChannelMigration:
    """Rewrite every endpoint channel still in the readable ``user.<uuid>`` form.

    An endpoint scoped to a user before pseudonymisation filters on ``user.<uuid>``, and no message
    carries that any more, so it would silently receive nothing while the API still reported its
    filter.  This is a ONE-SHOT operator step, run once per environment after upgrading
    (``scripts/data_migrations/pseudonymise_svix_user_channels.py``), not startup work.

    Idempotent: an endpoint already pseudonymous, unscoped, on a non-user channel, or on a token
    under a previous key is left alone, so a rerun migrates only what is still readable.  Any Svix
    failure PROPAGATES: an outage must not be reported as "nothing to migrate".
    """
    assert _client is not None
    applications = endpoints = legacy = migrated = 0
    app_iterator: str | None = None
    while True:
        apps = _client.application.list(ApplicationListOptions(limit=250, iterator=app_iterator))
        for app in apps.data:
            applications += 1
            ep_iterator: str | None = None
            while True:
                page = _client.endpoint.list(app.id, EndpointListOptions(limit=250, iterator=ep_iterator))
                for ep in page.data:
                    endpoints += 1
                    target = pseudonyms.pseudonymous_channels(ep.channels)
                    if ep.channels and target != list(ep.channels):
                        legacy += 1
                        if not dry_run:
                            _client.endpoint.patch(app.id, ep.id, EndpointPatch.model_validate({"channels": target}))
                            migrated += 1
                if page.done:
                    break
                ep_iterator = page.iterator
        if apps.done:
            break
        app_iterator = apps.iterator
    return LegacyChannelMigration(applications=applications, endpoints=endpoints, legacy=legacy, migrated=migrated)


def ensure_application(developer_id: str, developer_email: str) -> str:
    """Return the Svix application UID for a developer, creating it lazily.

    The Svix uid is set to the developer's UUID so no mapping is needed on our side.
    """
    if not is_enabled():
        return developer_id
    assert _client is not None
    uid = str(developer_id)
    try:
        _client.application.get_or_create(
            ApplicationIn(name=developer_email, uid=uid),
        )
    except httpx.ConnectError:
        logger.warning("Svix server unreachable — skipping application setup for developer %s", uid)
    except Exception:
        logger.exception("Failed to ensure Svix application for developer %s", uid)
    return uid


def send(
    event_type: str,
    developer_id: str,
    payload: dict[str, Any],
    *,
    channels: list[str] | None = None,
    idempotency_key: str | None = None,
) -> MessageOut | None:
    """Emit a webhook message via Svix. developer_id doubles as the Svix application UID."""
    if not is_enabled():
        return None
    assert _client is not None
    app_id = str(developer_id)
    event_id = pseudonyms.hash_event_id(idempotency_key)
    try:
        return _client.message.create(
            app_id,
            MessageIn(
                event_type=event_type,
                payload=payload,
                event_id=event_id,
                # Pseudonymised HERE as well as at the producers: a job enqueued by an earlier
                # release still carries the readable user.<uuid>, and this is the last point
                # before Svix stores it permanently.
                channels=pseudonyms.pseudonymous_channels(channels) or None,
                payload_retention_period=settings.svix_payload_retention_days,
            ),
        )
    except httpx.ConnectError:
        logger.warning(
            "Svix server unreachable — dropping event=%s for app=%s (no retry)",
            event_type,
            app_id,
        )
        return True  # type: ignore[return-value]  # truthy = don't count as failure  # ty:ignore[invalid-return-type]
    except HttpError as exc:
        if exc.status_code == 409:
            # Svix deduplication: the same event_id was already delivered.
            # Treat as success so the Celery task does not retry.
            # Log the digest, not the readable key, so the identifiers this change removes
            # from Svix are not reintroduced through the log.
            logger.debug(
                "Svix duplicate event_id=%s already delivered (409), skipping",
                event_id,
            )
            return True  # ty:ignore[invalid-return-type]
        logger.exception("Failed to send webhook event=%s to app=%s", event_type, app_id)
        return None
    except Exception:
        logger.exception("Failed to send webhook event=%s to app=%s", event_type, app_id)
        return None


def create_endpoint(
    app_id: str,
    url: str,
    description: str | None = None,
    filter_types: list[str] | None = None,
    *,
    user_id: UUID | None = None,
) -> EndpointOut:
    # Build via model_validate so that fields absent from the dict are NOT set
    # in model_fields_set.  EndpointIn uses exclude_unset=True serialisation;
    # passing channels=None explicitly would serialize as "channels":null and
    # Svix would treat it as "no channel = receive only untagged messages",
    # blocking all delivery (every message carries a user channel tag).
    assert _client is not None
    endpoint_data: dict[str, object] = {
        "url": url,
        "description": description or "",
    }
    if filter_types:
        endpoint_data["event_types"] = filter_types
    channels = pseudonyms.user_channels(user_id)
    if channels is not None:
        endpoint_data["channels"] = channels
    return _client.endpoint.create(
        app_id,
        EndpointIn.model_validate(endpoint_data),
    )


def list_endpoints(app_id: str, *, iterator: str | None = None) -> ListResponseEndpointOut:
    """One page of endpoints. Pass the previous response's `iterator` to walk the rest."""
    assert _client is not None
    return _client.endpoint.list(app_id, EndpointListOptions(iterator=iterator))


def has_endpoints(app_id: str) -> bool:
    """Return True when the developer's Svix application has at least one endpoint.

    The emit task uses this to skip developers who never registered an endpoint,
    so no payload is stored for an application that could not deliver it anyway.

    Only an answer that PROVES there is no endpoint returns False: an empty list, or a
    404 (the application was never created). A skip acknowledges the task with nothing
    sent and no retry, so every lookup that merely failed — Svix unreachable included —
    returns True and lets :func:`send` run. ``send`` owns the delivery-failure contract;
    deciding it here as well would turn a transient lookup error, which could clear
    before the message request, into a silently dropped event.
    """
    if not is_enabled():
        return False
    assert _client is not None
    try:
        return bool(_client.endpoint.list(app_id).data)
    except httpx.ConnectError as exc:
        log_and_capture_error(
            exc,
            logger,
            f"Svix server unreachable during endpoint lookup for app={app_id}; deferring to send",
            level="warning",
        )
        return True
    except HttpError as exc:
        if exc.status_code == 404:
            return False
        log_and_capture_error(exc, logger, f"Failed to list endpoints for app={app_id}; assuming it has endpoints")
        return True
    except Exception as exc:
        log_and_capture_error(exc, logger, f"Failed to list endpoints for app={app_id}; assuming it has endpoints")
        return True


def get_endpoint(app_id: str, endpoint_id: str) -> EndpointOut:
    assert _client is not None
    return _client.endpoint.get(app_id, endpoint_id)


def patch_endpoint(
    app_id: str,
    endpoint_id: str,
    *,
    url: str | None = None,
    description: str | None = None,
    filter_types: list[str] | None = None,
    user_id: UUID | None = None,
    clear_user_id: bool = False,
) -> EndpointOut:
    """Patch an endpoint.

    Pass ``user_id`` to scope the endpoint to a specific user.
    Pass ``clear_user_id=True`` (with ``user_id=None``) to remove an existing
    user scope and receive events for all users again.
    """
    assert _client is not None
    # Build via model_validate so only keys we actually want to update end up in
    # model_fields_set (EndpointPatch uses exclude_unset=True serialisation).
    # Rule: include a key → Svix UPDATES it (null = clear/remove the value).
    #       Omit a key → Svix LEAVES it unchanged.
    patch_data: dict[str, object] = {}
    if url is not None:
        patch_data["url"] = url
    if description is not None:
        patch_data["description"] = description
    if filter_types is not None:
        # [] is the public "remove the filter" signal (Svix rejects an empty list,
        # so it goes out as an explicit null).  None stays a no-op: it is what an
        # omitted field deserialises to and external clients already rely on that,
        # so it must not be repurposed into a second clearing signal.
        patch_data["event_types"] = filter_types or None
    if user_id is not None:
        patch_data["channels"] = pseudonyms.user_channels(user_id)
    elif clear_user_id:
        # Svix requires null (not []) to remove the channel filter entirely.
        patch_data["channels"] = None
    return _client.endpoint.patch(
        app_id,
        endpoint_id,
        EndpointPatch.model_validate(patch_data),
    )


def delete_endpoint(app_id: str, endpoint_id: str) -> None:
    assert _client is not None
    _client.endpoint.delete(app_id, endpoint_id)


def get_endpoint_secret(app_id: str, endpoint_id: str) -> str:
    """Return the signing secret for an endpoint so developers can verify payloads."""
    assert _client is not None
    result = _client.endpoint.get_secret(app_id, endpoint_id)
    return result.key


def get_message(app_id: str, msg_id: str) -> MessageOut | None:
    assert _client is not None
    try:
        return _client.message.get(app_id, msg_id)
    except Exception:
        logger.debug("Could not fetch message %s for app %s", msg_id, app_id)
        return None


def list_messages(
    app_id: str,
    options: MessageListOptions | None = None,
) -> ListResponseMessageOut:
    assert _client is not None
    return _client.message.list(app_id, options or MessageListOptions())


def list_message_attempts(
    app_id: str,
    endpoint_id: str,
    options: MessageAttemptListByEndpointOptions | None = None,
) -> ListResponseMessageAttemptOut:
    assert _client is not None
    return _client.message_attempt.list_by_endpoint(
        app_id, endpoint_id, options or MessageAttemptListByEndpointOptions()
    )


def send_test_message(app_id: str, endpoint_id: str, event_type: str) -> MessageOut | None:
    """Send a hardcoded sample event to a specific endpoint for testing.

    Uses ``message.create`` with an example payload instead of
    ``endpoint.send_example`` which requires Svix event-type schemas to be defined.
    The event_type is adjusted to match the endpoint's filters if set.
    """
    if not is_enabled():
        return None
    assert _client is not None
    try:
        ep = _client.endpoint.get(app_id, endpoint_id)
        if ep.event_types and event_type not in ep.event_types:
            event_type = ep.event_types[0]
        return _client.message.create(
            app_id,
            MessageIn(
                event_type=event_type,
                payload=get_test_payload(event_type),
                event_id=pseudonyms.hash_event_id(f"test.{endpoint_id}.{event_type}"),
                payload_retention_period=settings.svix_payload_retention_days,
            ),
        )
    except Exception:
        logger.exception("Failed to send test webhook event=%s to endpoint=%s", event_type, endpoint_id)
        return None
