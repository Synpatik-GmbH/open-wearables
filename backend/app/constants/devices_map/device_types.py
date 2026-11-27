"""Apple HealthKit source helpers.

FORK (port of upstream #1892): upstream 0.11.0 holds these two names in its own
``device_types.py`` beside its device-type tables; at the 0.11.0 upgrade take upstream's file.
"""

APPLE_SOURCE_APP_PREFIX = "com.apple.health."


def is_apple_own_source(source_app_id: str | None) -> bool:
    """HealthKit source written by an Apple device itself rather than a third-party app."""
    return source_app_id is not None and source_app_id.startswith(APPLE_SOURCE_APP_PREFIX)
