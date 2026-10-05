"""merge_upstream_0_9_0

Fork delta: joins the fork's refresh-token migration with upstream's chain at 0.9.0.

Both `7c3e9a41d2b8` (fork) and `7d6921a86914` (upstream) descend from `9f0940493a9b`.
Deployed fork databases already record `7c3e9a41d2b8`, so re-pointing it behind
upstream's head would make them treat upstream's migrations as applied and skip them.
A merge revision leaves both branches untouched: such a database runs upstream's
branch on `upgrade head`, and a fresh database runs both.

Revision ID: e4b7a1c9d3f2
Revises: a7c3e9f1b2d4, 7c3e9a41d2b8

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "e4b7a1c9d3f2"
down_revision: Union[str, Sequence[str], None] = ("a7c3e9f1b2d4", "7c3e9a41d2b8")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
