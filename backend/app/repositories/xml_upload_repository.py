"""2.96.9.2 D-04 statements for ``xml_uploads``; transactions belong to the lifecycle service."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import Row, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import User, XmlUpload
from app.models.wearable_history_source import WearableHistorySource


class XmlUploadRepository:
    def record_completed(self, db: Session, bucket: str, object_key: str, member: UUID) -> None:
        """One row per object; an unknown member writes nothing."""
        db.execute(
            insert(XmlUpload)
            .from_select(
                ["id", "user_id", "bucket", "object_key"],
                select(func.gen_random_uuid(), User.id, literal(bucket), literal(object_key)).where(User.id == member),
            )
            .on_conflict_do_nothing(constraint="uq_xml_uploads_object")
        )

    def mark_import(self, db: Session, bucket: str, object_key: str, state: str) -> None:
        db.execute(
            update(XmlUpload)
            .where(XmlUpload.bucket == bucket, XmlUpload.object_key == object_key, XmlUpload.state == "pending_import")
            .values(state=state, updated_at=func.now())
        )

    def fail_pending_older_than(self, db: Session, age: timedelta) -> list[Row]:
        return list(
            db.execute(
                update(XmlUpload)
                .where(XmlUpload.state == "pending_import", XmlUpload.created_at < func.now() - age)
                .values(state="failed", updated_at=func.now())
                .returning(XmlUpload.user_id, XmlUpload.object_key)
            ).all()
        )

    def processed_of_frozen_members(self, db: Session) -> list[Row]:
        return list(
            db.execute(
                select(XmlUpload.id, XmlUpload.user_id, XmlUpload.bucket, XmlUpload.object_key)
                .join(
                    WearableHistorySource,
                    (WearableHistorySource.user_id == XmlUpload.user_id) & (WearableHistorySource.provider == "apple"),
                )
                .where(
                    XmlUpload.state.in_(("imported", "failed")),
                    WearableHistorySource.freeze_at <= func.clock_timestamp(),
                )
            ).all()
        )

    def mark_deleted(self, db: Session, upload_id: UUID) -> None:
        db.execute(update(XmlUpload).where(XmlUpload.id == upload_id).values(state="deleted", updated_at=func.now()))
