"""2.96.9.2 D-04: completed S3 XML uploads are tracked, and deleted once the source is frozen."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import XmlUpload
from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.services.apple.apple_xml import xml_upload_lifecycle
from app.services.apple.apple_xml.xml_upload_lifecycle import (
    abort_stale_multipart_uploads,
    delete_processed_files,
    fail_stale_imports,
    mark_import,
    record_completed_upload,
)
from app.services.wearable_history_purge_service import WearableHistoryPurgeService
from tests.factories import UserFactory

BUCKET = "uploads"
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


class FakeS3:
    """Objects and multipart uploads in memory; a key in ``broken`` fails its call."""

    def __init__(self, uploads: list[dict[str, Any]] | None = None, broken: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self.aborted: list[str] = []
        self.uploads = uploads or []
        self.broken = broken or set()

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        if Key in self.broken:
            raise ConnectionError(Key)
        self.deleted.append(Key)

    def list_multipart_uploads(self, *, Bucket: str, Prefix: str = "", **_: Any) -> dict[str, Any]:  # noqa: N803
        live = [u for u in self.uploads if u["Key"].startswith(Prefix) and u["Key"] not in self.aborted]
        return {"Uploads": live, "IsTruncated": False}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> None:  # noqa: N803
        if Key in self.broken:
            raise ConnectionError(Key)
        self.aborted.append(Key)


def member(db: Session, *, frozen: bool | None) -> UUID:
    """A member whose Apple source is frozen, provisional, or absent (None)."""
    user = UserFactory()
    if frozen is not None:
        WearableHistoryRepository().begin_source(db, user.id, "apple", datetime(2026, 10, 1, 8, tzinfo=UTC))
        if not frozen:
            db.execute(
                text("UPDATE wearable_history_sources SET freeze_at=:f WHERE user_id=:m"),
                {"f": datetime.now(UTC) + timedelta(days=3), "m": user.id},
            )
    db.commit()
    return user.id


def states(db: Session, user_id: UUID) -> dict[str, str]:
    db.expire_all()
    return {r.object_key: r.state for r in db.scalars(select(XmlUpload).where(XmlUpload.user_id == user_id))}


def add(db: Session, user_id: UUID, key: str, state: str, age: timedelta = timedelta(0)) -> None:
    db.execute(
        text(
            "INSERT INTO xml_uploads (id, user_id, bucket, object_key, state, created_at) "
            "VALUES (:id, :u, :b, :k, :s, now() - :age)"
        ),
        {"id": uuid4(), "u": user_id, "b": BUCKET, "k": key, "s": state, "age": age},
    )
    db.commit()


def test_a_completed_upload_is_tracked_once_and_only_for_a_known_member(db: Session) -> None:
    user_id = member(db, frozen=None)
    record_completed_upload(db, BUCKET, f"{user_id}/raw/a.xml", str(user_id))
    record_completed_upload(db, BUCKET, f"{user_id}/raw/a.xml", str(user_id))
    record_completed_upload(db, BUCKET, "x/raw/b.xml", str(uuid4()))
    record_completed_upload(db, BUCKET, "y/raw/c.xml", "not-a-uuid")
    assert states(db, user_id) == {f"{user_id}/raw/a.xml": "pending_import"}
    assert db.scalar(text("SELECT count(*) FROM xml_uploads WHERE object_key IN ('x/raw/b.xml','y/raw/c.xml')")) == 0


def test_the_sns_trigger_tracks_the_upload_before_dispatching_it(db: Session) -> None:
    from app.services.apple.apple_xml import sns_service as module

    user_id = member(db, frozen=None)
    key = f"{user_id}/raw/a.xml"
    notification = MagicMock(
        message=json.dumps(
            {"Records": [{"eventSource": "aws:s3", "s3": {"bucket": {"name": BUCKET}, "object": {"key": key}}}]}
        )
    )
    with (
        patch.object(module.settings, "apple_xml_upload_completion_mode", "sns"),
        patch.object(module, "SessionLocal", return_value=nullcontext(db)),
        patch.object(module.process_aws_upload, "delay") as delay,
    ):
        module.SNSService()._process_s3_notification(notification)
    delay.assert_called_once()
    assert states(db, user_id) == {key: "pending_import"}


@pytest.mark.parametrize("outcome", ["imported", "failed"])
def test_the_client_completion_trigger_tracks_the_upload_and_its_import_outcome(db: Session, outcome: str) -> None:
    from app.integrations.celery.tasks import process_aws_upload_task as module

    user_id = member(db, frozen=None)
    key = f"{user_id}/raw/a.xml"
    task = MagicMock()
    task.request.id = "run"
    task.request.retries = 0
    task.request.called_directly = True
    seen: list[dict[str, str]] = []

    def process(bucket: str, object_key: str, user: str) -> dict[str, str]:
        seen.append(states(db, user_id))
        if outcome == "failed":
            raise RuntimeError("parse failed")
        return {"status": "success"}

    with (
        patch.object(module, "SessionLocal", side_effect=lambda: nullcontext(db)),
        patch.object(module, "_claim_import", return_value=("claimed", None)),
        patch.object(module, "_upload_completion_persisted", return_value=False),
        patch.object(module, "_ensure_upload_completed"),
        patch.object(module, "_store_upload_completion"),
        patch.object(module, "_store_completed_import"),
        patch.object(module, "_release_import_claim"),
        patch.object(module, "_process_aws_upload", side_effect=process),
        patch.object(module, "emit_sync_started"),
        patch.object(module, "emit_sync_completed"),
        patch.object(module, "emit_sync_failed"),
    ):
        run = lambda: module._run_import_task(  # noqa: E731
            task, bucket_name=BUCKET, object_key=key, user_id=str(user_id), upload_id="u", parts=[]
        )
        if outcome == "failed":
            with pytest.raises(RuntimeError):
                run()
        else:
            run()
    assert seen == [{key: "pending_import"}]
    assert states(db, user_id) == {key: outcome}


def test_a_late_import_outcome_never_moves_a_row_that_already_left_pending(db: Session) -> None:
    """A redelivered import after its file was deleted must not put the row back to wait."""
    user_id = member(db, frozen=None)
    add(db, user_id, "gone", "deleted")
    add(db, user_id, "stale", "failed")
    mark_import(db, BUCKET, "gone", "imported")
    mark_import(db, BUCKET, "stale", "imported")
    assert states(db, user_id) == {"gone": "deleted", "stale": "failed"}


def test_a_row_still_pending_after_a_day_is_failed_with_a_warning(db: Session, capsys: pytest.CaptureFixture) -> None:
    user_id = member(db, frozen=None)
    add(db, user_id, "old", "pending_import", timedelta(hours=25))
    add(db, user_id, "young", "pending_import", timedelta(hours=23))
    assert fail_stale_imports(db) == 1
    assert states(db, user_id) == {"old": "failed", "young": "pending_import"}
    assert "XML upload import never completed" in capsys.readouterr().out


def test_processed_files_of_frozen_members_are_deleted_and_a_failure_stays(
    db: Session, capsys: pytest.CaptureFixture
) -> None:
    frozen = member(db, frozen=True)
    provisional = member(db, frozen=False)
    for key, state in [("imp", "imported"), ("fail", "failed"), ("pend", "pending_import"), ("broken", "imported")]:
        add(db, frozen, key, state)
    add(db, provisional, "later", "imported")
    s3 = FakeS3(broken={"broken"})

    assert delete_processed_files(db, s3) == (2, 1)
    assert sorted(s3.deleted) == ["fail", "imp"]
    assert states(db, frozen) == {"imp": "deleted", "fail": "deleted", "pend": "pending_import", "broken": "imported"}
    assert states(db, provisional) == {"later": "imported"}
    assert "XML upload file deletion failed" in capsys.readouterr().out


def test_multipart_uploads_incomplete_for_a_day_are_aborted() -> None:
    s3 = FakeS3(
        uploads=[
            {"Key": "a/raw/old.xml", "UploadId": "1", "Initiated": NOW - timedelta(hours=25)},
            {"Key": "a/raw/young.xml", "UploadId": "2", "Initiated": NOW - timedelta(hours=23)},
            {"Key": "a/raw/broken.xml", "UploadId": "3", "Initiated": NOW - timedelta(hours=30)},
        ],
        broken={"a/raw/broken.xml"},
    )
    with patch.object(xml_upload_lifecycle.settings, "aws_bucket_name", BUCKET):
        assert abort_stale_multipart_uploads(s3, NOW) == (1, 1)
    assert s3.aborted == ["a/raw/old.xml"]


def test_the_apple_purge_waits_for_an_incomplete_multipart_upload(db: Session) -> None:
    user_id = member(db, frozen=True)
    s3 = FakeS3(uploads=[{"Key": f"{user_id}/raw/a.xml", "UploadId": "1", "Initiated": NOW}])
    with (
        patch.object(xml_upload_lifecycle.settings, "aws_bucket_name", BUCKET),
        patch("app.services.wearable_history_purge_service.get_s3_client", return_value=s3),
    ):
        assert WearableHistoryPurgeService().run(db).waiting == 1
        s3.aborted.append(f"{user_id}/raw/a.xml")
        assert WearableHistoryPurgeService().run(db).completed == 1


def test_the_cleanup_is_scheduled_every_fifteen_minutes_and_cleans(db: Session) -> None:
    from app.integrations.celery.core import create_celery
    from app.integrations.celery.tasks import wearable_history_purge_task as module

    entry = create_celery().conf.beat_schedule["clean-xml-uploads"]
    assert entry["task"] == module.clean_xml_uploads_task.name
    assert entry["schedule"].minute == set(range(0, 60, 15))
    user_id = member(db, frozen=True)
    add(db, user_id, "imp", "imported")
    add(db, user_id, "old", "pending_import", timedelta(hours=25))
    s3 = FakeS3(uploads=[{"Key": "z/raw/x.xml", "UploadId": "1", "Initiated": NOW - timedelta(days=2)}])
    with (
        patch.object(module, "SessionLocal", return_value=nullcontext(db)),
        patch.object(module, "get_s3_client", return_value=s3),
        patch.object(xml_upload_lifecycle.settings, "aws_bucket_name", BUCKET),
    ):
        assert module.clean_xml_uploads_task.run() == {
            "stale": 1,
            "deleted": 2,
            "failed": 0,
            "aborted": 1,
            "abort_failed": 0,
        }
    assert states(db, user_id) == {"imp": "deleted", "old": "deleted"}


def test_a_failed_multipart_listing_is_a_failure_and_not_waiting(db: Session) -> None:
    user_id = member(db, frozen=True)
    broken = MagicMock()
    broken.list_multipart_uploads.side_effect = ConnectionError("s3 down")
    with (
        patch.object(xml_upload_lifecycle.settings, "aws_bucket_name", BUCKET),
        patch("app.services.wearable_history_purge_service.get_s3_client", return_value=broken),
    ):
        run = WearableHistoryPurgeService().run(db)
    assert (run.failed, run.waiting) == (1, 0)
    source = WearableHistoryRepository().get(db, user_id, "apple")
    assert source is not None
    db.refresh(source)
    assert (source.purge_completed_at, source.purge_last_error) == (None, "uploads: ConnectionError")
