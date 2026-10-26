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
    codes = ", ".join(f"'{code}'" for code in TEMPERATURE_CODES)
    for table in ("data_point_series", "data_point_series_archive"):
        op.execute(
            f"DELETE FROM {table} WHERE series_type_definition_id IN "
            f"(SELECT id FROM series_type_definition WHERE code IN ({codes}))"
        )
    # Health scores that carried a temperature as a component. A score left with none stores NULL,
    # as a new write does (``without_not_kept_components``), not an empty object.
    for key in TEMPERATURE_COMPONENTS:
        op.execute(
            f"UPDATE health_score SET components = NULLIF(components - '{key}', '{{}}'::jsonb) "
            f"WHERE components ? '{key}'"
        )


def downgrade() -> None:
    # Deleted health data is not restored.
    pass
