"""refresh_token_rotated_from

Revision ID: 7c3e9a41d2b8
Revises: 9f0940493a9b

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c3e9a41d2b8"
down_revision: Union[str, None] = "9f0940493a9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refresh_token", sa.Column("rotated_from", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        "refresh_token_rotated_from_fkey",
        "refresh_token",
        "refresh_token",
        ["rotated_from"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint("refresh_token_rotated_from_key", "refresh_token", ["rotated_from"])


def downgrade() -> None:
    op.drop_constraint("refresh_token_rotated_from_key", "refresh_token", type_="unique")
    op.drop_constraint("refresh_token_rotated_from_fkey", "refresh_token", type_="foreignkey")
    op.drop_column("refresh_token", "rotated_from")
