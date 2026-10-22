"""2.96.9.2 wearable history freeze purge: purge state per source and XML upload rows.

Revision ID: c4e1d2b7a9f3
Revises: a29691004b01
Create Date: 2026-10-21 23:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "c4e1d2b7a9f3"
down_revision = "a29691004b01"
branch_labels = None
depends_on = None

XML_UPLOAD_STATES = ("pending_import", "imported", "failed", "deleted")


def upgrade() -> None:
    # D-03: an existing source starts with no purge recorded and no attempts.
    op.add_column("wearable_history_sources", sa.Column("records_purged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "wearable_history_sources", sa.Column("purge_completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "wearable_history_sources",
        sa.Column("purge_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("wearable_history_sources", sa.Column("purge_last_error", sa.Text(), nullable=True))
    # D-04: one row per completed S3 XML upload, so the file is deleted once processed.
    op.create_table(
        "xml_uploads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'pending_import'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"state IN {XML_UPLOAD_STATES}", name="ck_xml_uploads_state"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket", "object_key", name="uq_xml_uploads_object"),
    )
    op.create_index("ix_xml_uploads_user_state", "xml_uploads", ["user_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_xml_uploads_user_state", table_name="xml_uploads")
    op.drop_table("xml_uploads")
    for column in ("purge_last_error", "purge_attempts", "purge_completed_at", "records_purged_at"):
        op.drop_column("wearable_history_sources", column)
