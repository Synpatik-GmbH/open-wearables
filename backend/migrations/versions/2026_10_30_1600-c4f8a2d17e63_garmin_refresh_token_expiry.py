"""FORK (Calibra 2.71.3.3, design D-15): when a stored refresh token expires.

Null on every existing row: the expiry was never kept, and null reads as due, so the keep-alive's
first run refreshes them and learns it.

Revision ID: c4f8a2d17e63
Revises: b7e1c4a90d52
Create Date: 2026-10-30 16:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "c4f8a2d17e63"
down_revision = "b7e1c4a90d52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_connection", sa.Column("refresh_token_expires_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("user_connection", "refresh_token_expires_at")
