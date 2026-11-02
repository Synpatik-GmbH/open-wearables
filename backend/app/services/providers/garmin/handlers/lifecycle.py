"""Garmin connection lifecycle event handlers.

Handles webhook events that change the state of a user's Garmin connection:
- userPermissionsChange: update the OAuth scope stored on the connection, and end the connection
  when Health sharing is gone
- deregistrations: mark the connection as revoked

FORK (Calibra 2.71.3.3, design D-07): each push applies to every active row holding the Garmin
user id. Several Open Wearables users can hold the same one, and the first match alone would leave
the others ingesting after the member ended the connection.
"""

import logging
from typing import Any

from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.services.outgoing_webhooks.events import on_connection_revoked
from app.services.providers.garmin.deregistration import DeregistrationTrigger
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

HEALTH_PERMISSION = "HEALTH_EXPORT"


def ends_connection(entry: dict[str, Any]) -> bool:
    """Only losing Health sharing ends a connection (spec P14). An absent list says nothing."""
    permissions = entry.get("permissions")
    return isinstance(permissions, list) and HEALTH_PERMISSION not in permissions


def _scope_of(permissions: Any) -> str | None:
    """The stored scope. Only strings in a list join it: a stray member must not cost the entry."""
    if isinstance(permissions, list):
        permissions = [p for p in permissions if isinstance(p, str)]
    return " ".join(sorted(permissions)) if permissions else None


def _note_failure(
    db: DbSession,
    results: dict[str, Any],
    message: str,
    garmin_user_id: str,
    trace_id: str,
    error: Exception,
) -> None:
    """One entry's failure stays that entry's: the rest of the batch still has to end.

    The task that runs this reports success, so an entry abandoned here is never retried. Without
    this, a member who lost Health sharing after a bad entry in the same push would stay active.
    """
    db.rollback()
    results["errors"].append(f"{message}: {type(error).__name__}")
    log_structured(
        logger,
        "error",
        message,
        provider="garmin",
        trace_id=trace_id,
        garmin_user_id=garmin_user_id,
        error_type=type(error).__name__,
    )


def process_user_permissions(
    db: DbSession,
    connection_repo: UserConnectionRepository,
    oauth: BaseOAuthTemplate,
    permissions_list: list[dict[str, Any]],
    trace_id: str,
) -> dict[str, Any]:
    """Handle userPermissionsChange entries — update scope, and disconnect on losing Health."""
    # Imported here: loading the service loads the providers package, and so this module.
    from app.services.user_connection_service import user_connection_service  # noqa: PLC0415

    results: dict[str, Any] = {"updated": 0, "errors": []}

    if not isinstance(permissions_list, list):
        return {"updated": 0, "errors": ["Invalid userPermissions payload format"]}

    for entry in permissions_list:
        if not isinstance(entry, dict):
            results["errors"].append("Invalid userPermissions entry format")
            continue

        garmin_user_id: str | None = entry.get("userId")
        if not garmin_user_id:
            results["errors"].append("Missing userId in userPermissions entry")
            continue

        connections = connection_repo.get_all_by_provider_user_id(db, "garmin", garmin_user_id)
        if not connections:
            log_structured(
                logger,
                "info",
                "No connection found for Garmin user (userPermissions) — skipping",
                provider="garmin",
                trace_id=trace_id,
                garmin_user_id=garmin_user_id,
            )
            continue

        try:
            new_scope = _scope_of(entry.get("permissions", []))
        except Exception as e:
            _note_failure(db, results, "Failed to read a permissions entry", garmin_user_id, trace_id, e)
            continue

        for connection in connections:
            try:
                user_id = connection.user_id
                old_scope = connection.scope
                connection_repo.update_scope(db, connection, new_scope)

                log_structured(
                    logger,
                    "info",
                    "Updated user permissions scope",
                    provider="garmin",
                    trace_id=trace_id,
                    garmin_user_id=garmin_user_id,
                    user_id=str(user_id),
                    old_scope=old_scope,
                    new_scope=new_scope,
                )
                results["updated"] += 1

                if ends_connection(entry):
                    user_connection_service.disconnect(
                        db,
                        user_id,
                        "garmin",
                        oauth=oauth,
                        reason="permission_change",
                        trigger=DeregistrationTrigger.PERMISSION_CHANGE,
                    )
            except Exception as e:
                _note_failure(db, results, "Failed to apply a permission change", garmin_user_id, trace_id, e)

    return results


def process_deregistrations(
    db: DbSession,
    connection_repo: UserConnectionRepository,
    deregistrations_list: list[dict[str, Any]],
    trace_id: str,
) -> dict[str, Any]:
    """Handle deregistration entries — mark connections as revoked."""
    results: dict[str, Any] = {"revoked": 0, "errors": []}

    if not isinstance(deregistrations_list, list):
        return {"revoked": 0, "errors": ["Invalid deregistrations payload format"]}

    for entry in deregistrations_list:
        if not isinstance(entry, dict):
            results["errors"].append("Invalid deregistrations entry format")
            continue

        garmin_user_id: str | None = entry.get("userId")
        if not garmin_user_id:
            results["errors"].append("Missing userId in deregistrations entry")
            continue

        connections = connection_repo.get_all_by_provider_user_id(db, "garmin", garmin_user_id)
        if not connections:
            log_structured(
                logger,
                "info",
                "No connection found for Garmin user (deregistration) — skipping",
                provider="garmin",
                trace_id=trace_id,
                garmin_user_id=garmin_user_id,
            )
            continue

        for connection in connections:
            try:
                connection_repo.mark_as_revoked(db, connection)
                on_connection_revoked(
                    user_id=connection.user_id,
                    provider="garmin",
                    connection_id=connection.id,
                    reason="deregistration",
                    revoked_at=connection.updated_at.isoformat(),
                )

                log_structured(
                    logger,
                    "info",
                    "Revoked connection via deregistration webhook",
                    provider="garmin",
                    trace_id=trace_id,
                    garmin_user_id=garmin_user_id,
                    user_id=str(connection.user_id),
                )
                results["revoked"] += 1
            except Exception as e:
                _note_failure(db, results, "Failed to apply a deregistration", garmin_user_id, trace_id, e)

    return results
