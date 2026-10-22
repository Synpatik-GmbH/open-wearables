"""2.96.9.2 statements for the frozen-source purge; transactions belong to the purge service."""

from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.models import XmlUpload
from app.models.wearable_history_source import WearableHistorySource

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


class WearableHistoryPurgeRepository:
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

    def lock_due(self, db: Session, member: UUID, provider: str) -> WearableHistorySource | None:
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
        """D-01: rejected rows, then pre-start night evidence, in the caller's transaction."""
        counts = db.execute(_PURGE_RECORDS, {"member": source.user_id, "provider": source.provider}).one()
        db.execute(_TRIM_NIGHTS, {"id": source.id})
        return dict(counts._mapping)

    def stamp_records_purged(self, db: Session, source_id: UUID) -> None:
        db.execute(
            update(WearableHistorySource)
            .where(WearableHistorySource.id == source_id)
            .values(records_purged_at=func.clock_timestamp())
        )

    def record_failure(self, db: Session, member: UUID, provider: str, error: str, *, count_attempt: bool) -> None:
        db.execute(
            update(WearableHistorySource)
            .where(WearableHistorySource.user_id == member, WearableHistorySource.provider == provider)
            .values(
                purge_last_error=error,
                purge_attempts=WearableHistorySource.purge_attempts + (1 if count_attempt else 0),
            )
        )

    def mark_completed(self, db: Session, member: UUID, provider: str) -> None:
        db.execute(
            update(WearableHistorySource)
            .where(WearableHistorySource.user_id == member, WearableHistorySource.provider == provider)
            .values(purge_completed_at=func.clock_timestamp(), purge_last_error=None)
        )

    def has_tracked_upload(self, db: Session, member: UUID) -> bool:
        """An upload of the member whose file is not yet confirmed deleted."""
        tracked = db.scalar(
            select(XmlUpload.id)
            .where(XmlUpload.user_id == member, XmlUpload.state.in_(_WAITING_UPLOAD_STATES))
            .limit(1)
        )
        return tracked is not None
