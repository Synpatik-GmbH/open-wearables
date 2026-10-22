"""2.96.9.2 D-04: every completed S3 XML upload is tracked until its file is deleted.

A row is written where an upload completes, moves to ``imported`` or ``failed`` with its
import, and its object is deleted once the member's Apple source is frozen. Incomplete
multipart uploads are aborted after a day. D-03 waits on both before an Apple source's purge
is complete.
"""

from datetime import UTC, datetime, timedelta
from logging import getLogger
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.xml_upload_repository import XmlUploadRepository
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

STALE_AFTER = timedelta(hours=24)

uploads = XmlUploadRepository()


def _member(user_id: str) -> UUID | None:
    try:
        return UUID(user_id)
    except (TypeError, ValueError):
        return None


def record_completed_upload(db: Session, bucket: str, object_key: str, user_id: str) -> None:
    """Track a completed upload once; an unknown member has no history to purge and no row."""
    member = _member(user_id)
    if member is None:
        return
    uploads.record_completed(db, bucket, object_key, member)
    db.commit()


def mark_import(db: Session, bucket: str, object_key: str, state: str) -> None:
    """Record an import's outcome on a row still waiting for it."""
    uploads.mark_import(db, bucket, object_key, state)
    db.commit()


def fail_stale_imports(db: Session) -> int:
    """A row still pending a day after it was written never got its import's outcome."""
    rows = uploads.fail_pending_older_than(db, STALE_AFTER)
    db.commit()
    for row in rows:
        log_structured(
            logger,
            "warning",
            "XML upload import never completed",
            provider="apple_xml",
            user_id=str(row.user_id),
            object_key=row.object_key,
        )
    return len(rows)


def delete_processed_files(db: Session, s3: Any) -> tuple[int, int]:
    """Delete the file of every processed upload whose member's Apple source is frozen."""
    rows = uploads.processed_of_frozen_members(db)
    deleted = failed = 0
    for row in rows:
        try:
            # S3 answers a missing key with success, so "already absent" is a confirmed deletion.
            s3.delete_object(Bucket=row.bucket, Key=row.object_key)
        except Exception as error:
            failed += 1
            log_structured(
                logger,
                "error",
                "XML upload file deletion failed",
                provider="apple_xml",
                user_id=str(row.user_id),
                object_key=row.object_key,
                error=type(error).__name__,
            )
            continue
        uploads.mark_deleted(db, row.id)
        db.commit()
        deleted += 1
    return deleted, failed


def _incomplete_uploads(s3: Any, prefix: str = "") -> list[dict[str, Any]]:
    uploads: list[dict[str, Any]] = []
    request: dict[str, Any] = {"Bucket": settings.aws_bucket_name, "Prefix": prefix}
    while True:
        page = s3.list_multipart_uploads(**request)
        uploads.extend(page.get("Uploads", []))
        if not page.get("IsTruncated"):
            return uploads
        request["KeyMarker"] = page["NextKeyMarker"]
        request["UploadIdMarker"] = page["NextUploadIdMarker"]


def abort_stale_multipart_uploads(s3: Any, now: datetime | None = None) -> tuple[int, int]:
    """Abort every multipart upload still incomplete a day after it began."""
    cutoff = (now or datetime.now(UTC)) - STALE_AFTER
    aborted = failed = 0
    for upload in _incomplete_uploads(s3):
        if upload["Initiated"] >= cutoff:
            continue
        try:
            s3.abort_multipart_upload(Bucket=settings.aws_bucket_name, Key=upload["Key"], UploadId=upload["UploadId"])
        except Exception as error:
            failed += 1
            log_structured(
                logger,
                "error",
                "Incomplete XML upload abort failed",
                provider="apple_xml",
                object_key=upload["Key"],
                error=type(error).__name__,
            )
            continue
        aborted += 1
    return aborted, failed


def member_has_incomplete_upload(s3: Any | None, member: UUID) -> bool:
    """Upload keys are namespaced ``<user_id>/``; without storage there are no uploads."""
    if s3 is None or not settings.aws_bucket_name:
        return False
    return bool(_incomplete_uploads(s3, f"{member}/"))
