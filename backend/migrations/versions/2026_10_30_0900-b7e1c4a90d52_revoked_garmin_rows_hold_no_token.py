"""FORK (Calibra 2.71.3.3, design D-05, INV-05): revoked Garmin rows hold no token.

A disconnect always cleared the tokens; a deregistration push and a rejected refresh kept them. From
this release both clear them too. This clears what those two left behind, so that a revoked row
cannot earn a deregistration attempt at account deletion for a connection that had already ended.

updated_at is left alone: it is the instant connection.revoked reported as the end.

Revision ID: b7e1c4a90d52
Revises: 9d3b6f2e8a41
Create Date: 2026-10-30 09:00:00
"""

from alembic import op

revision = "b7e1c4a90d52"
down_revision = "9d3b6f2e8a41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE user_connection
           SET access_token = NULL,
               refresh_token = NULL,
               token_expires_at = NULL
         WHERE provider = 'garmin'
           AND status = 'revoked'
           AND (access_token IS NOT NULL OR refresh_token IS NOT NULL OR token_expires_at IS NOT NULL)
        """
    )


def downgrade() -> None:
    """Nothing to restore: the tokens are gone, and they were dead."""
