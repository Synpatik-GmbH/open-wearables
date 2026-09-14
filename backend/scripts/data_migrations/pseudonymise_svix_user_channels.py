#!/usr/bin/env python3
"""Move Svix endpoints scoped to a user off the readable ``user.<uuid>`` channel.

Run ONCE per environment after deploying pseudonymous user channels. An endpoint scoped to a user
before that deploy filters on the readable channel, which no message carries any more, so it
receives nothing until migrated. Idempotent: a rerun migrates only what is still readable.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/pseudonymise_svix_user_channels.py --dry-run
    docker compose exec app uv run python scripts/data_migrations/pseudonymise_svix_user_channels.py

Exit 0 with the counts when the run completed (``legacy: 0`` means nothing needed migrating).
Exit 1 when Svix is not configured or any request failed; nothing is lost, rerun it.
"""

import argparse
import sys

from app.services.outgoing_webhooks import svix as svix_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate Svix endpoints off readable user channels.")
    parser.add_argument("--dry-run", action="store_true", help="Count legacy endpoints without changing them.")
    args = parser.parse_args(argv)

    if not svix_service.is_enabled():
        print("Svix is not configured (set SVIX_JWT_SECRET or SVIX_AUTH_TOKEN); nothing was checked.", file=sys.stderr)
        return 1

    try:
        result = svix_service.migrate_legacy_user_channels(dry_run=args.dry_run)
    except Exception as exc:
        print(
            f"FAILED: {type(exc).__name__}: {exc}. Endpoints already migrated stay migrated; rerun the script.",
            file=sys.stderr,
        )
        return 1

    mode = "dry run — nothing changed" if args.dry_run else "applied"
    print(
        f"Svix user-channel migration ({mode}): applications: {result.applications}, "
        f"endpoints: {result.endpoints}, legacy: {result.legacy}, migrated: {result.migrated}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
