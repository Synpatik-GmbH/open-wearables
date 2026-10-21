from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey

XML_UPLOAD_STATES = ("pending_import", "imported", "failed", "deleted")


class XmlUpload(BaseDbModel):
    """2.96.9.2 D-04: a completed S3 XML upload, tracked until its file is deleted."""

    __tablename__ = "xml_uploads"
    __table_args__ = (
        CheckConstraint(f"state IN {XML_UPLOAD_STATES}", name="ck_xml_uploads_state"),
        UniqueConstraint("bucket", "object_key", name="uq_xml_uploads_object"),
        Index("ix_xml_uploads_user_state", "user_id", "state"),
    )

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending_import'"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
