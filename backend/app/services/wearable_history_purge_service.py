"""2.96.9.2 (wearable history freeze purge): delete a frozen source's pre-start history.

D-01: one transaction deletes every row of the source that its own read predicate rejects,
each decided against the state before any change. D-03: that transaction sets
``records_purged_at`` and is never rolled back by a later step; the source is complete only
when every temporary store is confirmed too. Failures are recorded and retried every run.
"""

from dataclasses import dataclass
from logging import getLogger
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.wearable_history_source import WearableHistorySource
from app.repositories.wearable_history_purge_repository import WearableHistoryPurgeRepository
from app.services.apple.apple_xml.aws_service import get_s3_client
from app.services.apple.apple_xml.xml_upload_lifecycle import member_has_incomplete_upload
from app.services.sdk.sleep_service import delete_sleep_state, load_sleep_state
from app.services.sdk.sleep_stages import normalized_sleep_window
from app.utils.structured_logging import log_structured
from app.utils.wearable_history_clock import BERLIN, recorded_night_date

logger = getLogger(__name__)


@dataclass
class PurgeRun:
    due: int = 0
    completed: int = 0
    failed: int = 0
    waiting: int = 0


class WearableHistoryPurgeService:
    def __init__(self) -> None:
        self.repository = WearableHistoryPurgeRepository()

    def purge_records(self, db: Session, source: WearableHistorySource) -> dict[str, int]:
        """D-01 in the caller's transaction: rejected rows, then pre-start night evidence."""
        return self.repository.purge_records(db, source)

    def stamp_records_purged(self, db: Session, source_id: UUID) -> None:
        """D-03: written after the D-01 commit, so a read that starts after the stamp sees the
        purged state; an export requested before it is deleted by the adapter's cutoff."""
        self.repository.stamp_records_purged(db, source_id)

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
        return self.repository.has_tracked_upload(db, member) or member_has_incomplete_upload(get_s3_client(), member)

    def _fail(
        self, db: Session, member: UUID, provider: str, step: str, error: Exception, *, count_attempt: bool = False
    ) -> None:
        """Record a failure after rolling back; a rolled-back records step still counts its attempt."""
        db.rollback()
        self.repository.record_failure(
            db, member, provider, f"{step}: {type(error).__name__}", count_attempt=count_attempt
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
            source = self.repository.lock_due(db, member, provider)
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
                db.refresh(source)
            except Exception as error:
                self._fail(db, member, provider, "records", error)
                return "failed"

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
        try:
            self.repository.mark_completed(db, member, provider)
            db.commit()
        except Exception as error:
            self._fail(db, member, provider, "complete", error)
            return "failed"
        return "completed"

    def run(self, db: Session) -> PurgeRun:
        run = PurgeRun()
        for member, provider in self.repository.due_sources(db):
            run.due += 1
            outcome = self.purge_source(db, member, provider)
            if outcome != "skipped":
                setattr(run, outcome, getattr(run, outcome) + 1)
        return run


wearable_history_purge_service = WearableHistoryPurgeService()
