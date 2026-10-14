"""polar_true_time_and_registration

FORK (2.96.6.1, No lost wearable data): three operational tables, identifiers and state only
(R-2026-10-13-e), each deleted with the user through its foreign key.

- polar_workout_true_time: which Polar workouts hold their true start instant (spec L2).
- polar_registration: whether Polar accepted the connection's user registration (spec cause 4).
- polar_pull_state: when Calibra last pulled Polar for the connection, when each data type was
  last fetched in full (spec R1, Polar's call limit), and when and how often that day it asked
  Polar for last night because the member opened the app (spec R2).

Revision ID: 635d0674389f
Revises: e4b7a1c9d3f2

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "635d0674389f"
down_revision: Union[str, None] = "e4b7a1c9d3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "polar_workout_true_time",
        sa.Column("event_record_id", sa.UUID(), nullable=False),
        sa.Column("recorded_by", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["event_record_id"], ["event_record.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_record_id"),
    )
    op.create_table(
        "polar_registration",
        sa.Column("user_connection_id", sa.UUID(), nullable=False),
        sa.Column("registered", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_connection_id"], ["user_connection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_connection_id"),
    )
    op.create_table(
        "polar_pull_state",
        sa.Column("user_connection_id", sa.UUID(), nullable=False),
        sa.Column("pull_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "full_fetched_at",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("on_open_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("on_open_day", sa.Date(), nullable=True),
        sa.Column("on_open_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_connection_id"], ["user_connection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_connection_id"),
    )


def downgrade() -> None:
    op.drop_table("polar_pull_state")
    op.drop_table("polar_registration")
    op.drop_table("polar_workout_true_time")
