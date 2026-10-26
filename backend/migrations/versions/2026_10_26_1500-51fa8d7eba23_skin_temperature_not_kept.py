"""FORK (Calibra rulings R-2026-10-23-f, R-2026-10-26-d): delete the skin and body temperature held.

Skin temperature is not kept: stored without a purpose and close to inferring a woman's cycle
(DPO point 9); body temperature, from every source, goes with it. From this release no write
stores either (``NOT_KEPT_SERIES``, ``NOT_KEPT_COMPONENTS``); this deletes what was stored
before, from the live series and from the daily archive, for every member and provider, and
the temperature components of health scores (WHOOP recovery's skin_temp_celsius, Oura
readiness's body_temperature). The codes are written out, not imported, so this migration
means the same thing whatever the code later says.

Revision ID: 51fa8d7eba23
Revises: c4e1d2b7a9f3
Create Date: 2026-10-26 15:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "51fa8d7eba23"
down_revision = "c4e1d2b7a9f3"
branch_labels = None
depends_on = None

TEMPERATURE_CODES = (
    "skin_temperature",
    "skin_temperature_deviation",
    "skin_temperature_trend_deviation",
    "garmin_skin_temperature",
    "body_temperature",
)
TEMPERATURE_COMPONENTS = ("skin_temp_celsius", "body_temperature")


def upgrade() -> None:
    # This runs when the API starts. A worker still on the old image can hold a lock on these
    # tables; fail fast (the deploy is retried) rather than hold the API start, and every write
    # queued behind this migration, until that worker lets go. Reset at the end for whatever
    # runs after this in the same transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    codes = ", ".join(f"'{code}'" for code in TEMPERATURE_CODES)
    for table in ("data_point_series", "data_point_series_archive"):
        op.execute(
            f"DELETE FROM {table} WHERE series_type_definition_id IN "
            f"(SELECT id FROM series_type_definition WHERE code IN ({codes}))"
        )
    # Health scores that carried a temperature as a component. A score left with none stores JSON
    # null, which is what the ORM writes for a score with no components, not an empty object.
    # The wearable-history guard (wh_ingress, a29691004b01) checks every updated score and refuses
    # one it would not admit today (pre-start for a frozen source, or a provider with no source
    # beside a member's other sources). Removing a component only reduces what is held, so the
    # guard is off for these statements alone, inside the migration's transaction. Checks already
    # queued in this transaction are run first (they would run at commit anyway), because a table
    # with pending trigger events cannot be altered. Turning the guard off locks health_score
    # against every writer, so it is done only where a score holds such a component: where none
    # does (prod held none), an old-image worker writing scores during the rollout is never waited on.
    keys = ", ".join(f"'{key}'" for key in TEMPERATURE_COMPONENTS)
    held = sa.text(f"SELECT EXISTS (SELECT 1 FROM health_score WHERE components ?| ARRAY[{keys}])")
    if op.get_bind().execute(held).scalar():
        op.execute("SET CONSTRAINTS wh_ingress IMMEDIATE")
        op.execute("ALTER TABLE health_score DISABLE TRIGGER wh_ingress")
        for key in TEMPERATURE_COMPONENTS:
            op.execute(
                f"UPDATE health_score SET components = CASE WHEN components - '{key}' = '{{}}'::jsonb "
                f"THEN 'null'::jsonb ELSE components - '{key}' END WHERE components ? '{key}'"
            )
        op.execute("ALTER TABLE health_score ENABLE TRIGGER wh_ingress")
        # Alembic runs every pending migration in one transaction: hand the guard back as it was
        # declared, checking at commit, for whatever runs after this.
        op.execute("SET CONSTRAINTS wh_ingress DEFERRED")
    op.execute("SET LOCAL lock_timeout TO DEFAULT")


def downgrade() -> None:
    # Deleted health data is not restored.
    pass
