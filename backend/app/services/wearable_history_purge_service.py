"""2.96.9.2 (wearable history freeze purge): delete a frozen source's pre-start history.

D-01: one transaction deletes every row of the source that its own read predicate rejects,
each decided against the state before any change. D-03: that transaction sets
``records_purged_at`` and is never rolled back by a later step; the source is complete only
when every temporary store is confirmed too. Failures are recorded and retried every run.
"""

from dataclasses import dataclass
from logging import getLogger
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.models import XmlUpload
from app.models.wearable_history_source import WearableHistorySource
from app.services.apple.apple_xml.aws_service import get_s3_client
from app.services.apple.apple_xml.xml_upload_lifecycle import member_has_incomplete_upload
from app.services.sdk.sleep_service import delete_sleep_state, load_sleep_state
from app.services.sdk.sleep_stages import normalized_sleep_window
from app.utils.structured_logging import log_structured
from app.utils.wearable_history_clock import BERLIN, recorded_night_date

logger = getLogger(__name__)

# Every predicate reads the snapshot taken when the statement starts, so no deletion below
# can change another row's verdict (D-01 step 1). Sleep intervals are what wh_read_sample
# reads for night physiology, and they are deleted in the same statement.
_PURGE_RECORDS = text(
    """
    WITH f_sources AS (
        SELECT id FROM data_source WHERE user_id = :member AND provider::text = :provider
    ),
    s AS (
        SELECT d.id FROM data_point_series d
        WHERE d.data_source_id IN (SELECT id FROM f_sources)
          AND NOT wh_read_sample(d.data_source_id, d.series_type_definition_id, d.recorded_at)
    ),
    e AS (
        SELECT r.id FROM event_record r
        WHERE r.data_source_id IN (SELECT id FROM f_sources)
          AND r.category IN ('sleep', 'workout')
          AND NOT wh_read_event(r.data_source_id, r.category, r.start_datetime, r.end_datetime, r.zone_offset)
    ),
    h AS (
        SELECT h.id FROM health_score h
        WHERE (h.data_source_id IN (SELECT id FROM f_sources)
               OR (h.data_source_id IS NULL AND h.user_id = :member AND h.provider::text = :provider))
          AND (h.event_record_id IS NULL OR h.event_record_id NOT IN (SELECT id FROM e))
          AND NOT wh_read_score(h.user_id, h.data_source_id, h.provider::text, h.event_record_id, h.recorded_at)
    ),
    a AS (
        SELECT x.id FROM data_point_series_archive x
        WHERE x.data_source_id IN (SELECT id FROM f_sources)
          AND NOT wh_read_archive(x.data_source_id, x.series_type_definition_id, x.bucket_start_at,
                                  x.history_start_date)
    ),
    ds AS (DELETE FROM data_point_series WHERE id IN (SELECT id FROM s) RETURNING 1),
    de AS (DELETE FROM event_record WHERE id IN (SELECT id FROM e) RETURNING 1),
    dh AS (DELETE FROM health_score WHERE id IN (SELECT id FROM h) RETURNING 1),
    da AS (DELETE FROM data_point_series_archive WHERE id IN (SELECT id FROM a) RETURNING 1)
    SELECT (SELECT count(*) FROM ds) AS samples, (SELECT count(*) FROM de) AS events,
           (SELECT count(*) FROM dh) AS scores, (SELECT count(*) FROM da) AS archives
    """
)

_TRIM_NIGHTS = text(
    """
    UPDATE wearable_history_sources
    SET night_dates = ARRAY(SELECT d FROM unnest(night_dates) AS d WHERE d >= start_date ORDER BY d)
    WHERE id = :id
    """
)

_WAITING_UPLOAD_STATES = ("pending_import", "imported", "failed")


@dataclass
class PurgeRun:
    due: int = 0
    completed: int = 0
    failed: int = 0
    waiting: int = 0


class WearableHistoryPurgeService:
    def due_sources(self, db: Session) -> list[tuple[UUID, str]]:
        rows = db.execute(
            select(WearableHistorySource.user_id, WearableHistorySource.provider)
            .where(
                WearableHistorySource.freeze_at <= func.clock_timestamp(),
                WearableHistorySource.purge_completed_at.is_(None),
            )
            .order_by(WearableHistorySource.records_purged_at.is_not(None), WearableHistorySource.freeze_at)
        ).all()
        return [(row.user_id, row.provider) for row in rows]

    def _lock(self, db: Session, member: UUID, provider: str) -> WearableHistorySource | None:
        """The source row, locked as ingestion locks it; None when another run holds it or it is done."""
        return db.scalar(
            select(WearableHistorySource)
            .where(
                WearableHistorySource.user_id == member,
                WearableHistorySource.provider == provider,
                WearableHistorySource.freeze_at <= func.clock_timestamp(),
                WearableHistorySource.purge_completed_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )

    def purge_records(self, db: Session, source: WearableHistorySource) -> dict[str, int]:
        """D-01 in the caller's transaction: rejected rows, then pre-start night evidence."""
        counts = db.execute(_PURGE_RECORDS, {"member": source.user_id, "provider": source.provider}).one()
        db.execute(_TRIM_NIGHTS, {"id": source.id})
        return dict(counts._mapping)

    def stamp_records_purged(self, db: Session, source_id: UUID) -> None:
        """D-03: written after the D-01 commit, so a read that starts after the stamp sees the
        purged state; an export requested before it is deleted by the adapter's cutoff."""
        db.execute(
            update(WearableHistorySource)
            .where(WearableHistorySource.id == source_id)
            .values(records_purged_at=func.clock_timestamp())
        )

    def clear_sleep_state(self, source: WearableHistorySource) -> None:
        """D-02: the Apple sleep buffer, when the night it would record is before the start date."""
        state = load_sleep_state(str(source.user_id))
        if state is None:
            return
        start, end = normalized_sleep_window(state.stages, state.start_time, state.end_time)
        night = recorded_night_date(start, end, state.zone_offset) or start.astimezone(BERLIN).date()
        if night < source.start_date:
            delete_sleep_state(str(source.user_id))

    def uploads_waiting(self, db: Session, member: UUID) -> bool:
        """D-03: an Apple source completes only once no upload file of the member is left."""
        tracked = db.scalar(
            select(XmlUpload.id)
            .where(XmlUpload.user_id == member, XmlUpload.state.in_(_WAITING_UPLOAD_STATES))
            .limit(1)
        )
        return tracked is not None or member_has_incomplete_upload(get_s3_client(), member)

    def _fail(
        self, db: Session, member: UUID, provider: str, step: str, error: Exception, *, count_attempt: bool = False
    ) -> None:
        """Record a failure after rolling back; a rolled-back records step still counts its attempt."""
        db.rollback()
        db.execute(
            update(WearableHistorySource)
            .where(WearableHistorySource.user_id == member, WearableHistorySource.provider == provider)
            .values(
                purge_last_error=f"{step}: {type(error).__name__}",
                purge_attempts=WearableHistorySource.purge_attempts + (1 if count_attempt else 0),
            )
        )
        db.commit()
        log_structured(
            logger,
            "error",
            "Wearable history purge failed",
            provider=provider,
            user_id=str(member),
            step=step,
            error=type(error).__name__,
        )

    def purge_source(self, db: Session, member: UUID, provider: str) -> str:
        """One run for one source: ``completed``, ``failed``, ``waiting`` or ``skipped``."""
        try:
            source = self._lock(db, member, provider)
            if source is None:
                db.rollback()
                return "skipped"
            source.purge_attempts += 1
            stamp = source.records_purged_at is None
            if stamp:
                # Re-running after a failed stamp is safe: nothing rejected is left to delete.
                self.purge_records(db, source)
            source_id = source.id
            db.commit()
        except Exception as error:
            self._fail(db, member, provider, "records", error, count_attempt=True)
            return "failed"
        if stamp:
            try:
                self.stamp_records_purged(db, source_id)
                db.commit()
            except Exception as error:
                self._fail(db, member, provider, "records", error)
                return "failed"
            db.refresh(source)

        failed = False
        waiting = False
        if provider == "apple":
            try:
                self.clear_sleep_state(source)
            except Exception as error:
                self._fail(db, member, provider, "sleep_state", error)
                failed = True
            try:
                waiting = self.uploads_waiting(db, member)
            except Exception as error:
                self._fail(db, member, provider, "uploads", error)
                failed = True
        if failed or waiting:
            # Waiting is not a failure and records nothing (D-03).
            db.rollback()
            return "failed" if failed else "waiting"
        db.execute(
            update(WearableHistorySource)
            .where(WearableHistorySource.user_id == member, WearableHistorySource.provider == provider)
            .values(purge_completed_at=func.clock_timestamp(), purge_last_error=None)
        )
        db.commit()
        return "completed"

    def run(self, db: Session) -> PurgeRun:
        run = PurgeRun()
        for member, provider in self.due_sources(db):
            run.due += 1
            outcome = self.purge_source(db, member, provider)
            if outcome != "skipped":
                setattr(run, outcome, getattr(run, outcome) + 1)
        return run


wearable_history_purge_service = WearableHistoryPurgeService()
