from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID, uuid4

import isodate

from app.constants.workout_types.polar import get_unified_workout_type
from app.database import DbSession
from app.models import PolarWorkoutTrueTime
from app.schemas.enums import ProviderName
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
)
from app.schemas.providers.polar import ExerciseJSON as PolarExerciseJSON
from app.services.event_record_service import event_record_service
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.utils.dates import offset_to_iso

# FORK (2.96.6.1, spec Q5): nothing whose date is more than this many days back is fetched or stored.
POLAR_WINDOW_DAYS = 90


class PolarWorkouts(BaseWorkoutsTemplate):
    """Polar implementation of workouts template."""

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Any]:
        """Get exercises from Polar API."""
        return self._make_api_request(db, user_id, "/v3/exercises")

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Get exercises from Polar API with options."""
        samples = kwargs.get("samples", False)
        zones = kwargs.get("zones", False)
        route = kwargs.get("route", False)

        params = {
            "samples": str(samples).lower(),
            "zones": str(zones).lower(),
            "route": str(route).lower(),
        }
        return self._make_api_request(db, user_id, "/v3/exercises", params=params)

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        """Get detailed exercise data from Polar API."""
        samples = kwargs.get("samples", False)
        zones = kwargs.get("zones", False)
        route = kwargs.get("route", False)
        return self.get_exercise_detail(db, user_id, workout_id, samples, zones, route)

    def _extract_dates(self, start_timestamp: Any, end_timestamp: Any) -> tuple[datetime, datetime]:
        """Extract start and end dates from timestamps.

        Note: Polar uses a different format with offset, so this delegates to _extract_dates_with_offset.
        This is required by the base template but not used directly.
        """
        raise NotImplementedError("Use _extract_dates_with_offset for Polar workouts")

    def _extract_dates_with_offset(
        self,
        start_time: str,
        start_time_utc_offset: int,
        duration: str,
    ) -> tuple[datetime, datetime]:
        """Extract start and end instants (UTC-aware) from Polar's local start time and offset.

        FORK (2.96.6.1, spec N9): Polar's ``start_time`` is local wall-clock time with no zone, so the
        true instant is local MINUS ``start_time_utc_offset``. This used to ADD it and return a naive
        value, which Postgres then read in the session time zone: every stored exercise sat two
        offsets late. A ``start_time`` that already carries a zone is converted as given and the
        offset is not applied a second time.
        """
        parsed = isodate.parse_datetime(start_time)
        if parsed.tzinfo is None:
            start_date = (parsed - timedelta(minutes=start_time_utc_offset)).replace(tzinfo=timezone.utc)
        else:
            start_date = parsed.astimezone(timezone.utc)
        duration_td = isodate.parse_duration(duration)
        end_date = start_date + duration_td
        return start_date, end_date

    def _build_metrics(self, raw_workout: PolarExerciseJSON) -> EventRecordMetrics:
        hr_avg = (
            Decimal(str(raw_workout.heart_rate.average))
            if raw_workout.heart_rate and raw_workout.heart_rate.average is not None
            else None
        )
        hr_max = (
            Decimal(str(raw_workout.heart_rate.maximum))
            if raw_workout.heart_rate and raw_workout.heart_rate.maximum is not None
            else None
        )

        energy_burned = Decimal(str(raw_workout.calories)) if raw_workout.calories is not None else None

        distance = Decimal(str(raw_workout.distance)) if raw_workout.distance is not None else None

        return {
            "heart_rate_max": int(hr_max) if hr_max is not None else None,
            "heart_rate_avg": hr_avg,
            "energy_burned": energy_burned,
            "distance": distance,
        }

    def _normalize_workout(
        self,
        raw_workout: PolarExerciseJSON,
        user_id: UUID,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Normalize Polar exercise to EventRecordCreate and EventRecordDetailCreate."""
        workout_id = uuid4()

        workout_type = get_unified_workout_type(raw_workout.sport, raw_workout.detailed_sport_info)

        start_date, end_date = self._extract_dates_with_offset(
            raw_workout.start_time,
            raw_workout.start_time_utc_offset,
            raw_workout.duration,
        )
        duration_seconds = int((end_date - start_date).total_seconds())

        metrics = self._build_metrics(raw_workout)

        # convert from offset minutes to seconds first
        zone_offset = offset_to_iso(raw_workout.start_time_utc_offset * 60)

        record = EventRecordCreate(
            category="workout",
            type=workout_type.value,
            source_name=raw_workout.device,
            device_model=raw_workout.device,
            duration_seconds=duration_seconds,
            start_datetime=start_date,
            end_datetime=end_date,
            zone_offset=zone_offset,
            id=workout_id,
            external_id=raw_workout.id,
            source="polar",
            user_id=user_id,
        )

        detail = EventRecordDetailCreate(
            record_id=workout_id,
            **metrics,
        )

        return record, detail

    def _build_bundles(
        self,
        raw: list[PolarExerciseJSON],
        user_id: UUID,
    ) -> Iterable[tuple[EventRecordCreate, EventRecordDetailCreate]]:
        """Build event record payloads for Polar exercises."""
        for raw_workout in raw:
            yield self._normalize_workout(raw_workout, user_id)

    def load_data(
        self,
        db: DbSession,
        user_id: UUID,
        **kwargs: Any,
    ) -> int:
        """Load data from Polar API."""
        workouts_data = self.get_workouts_from_api(db, user_id, **kwargs)
        return self._save_exercises(db, user_id, [PolarExerciseJSON(**w) for w in workouts_data])

    def fetch_and_save_exercise(self, db: DbSession, user_id: UUID, path: str) -> int:
        """Fetch a single exercise by URL path and save it. Used by webhook handler."""
        raw = self._make_api_request(db, user_id, path)
        if not raw:
            return 0
        return self._save_exercises(db, user_id, [PolarExerciseJSON(**raw)])

    def _save_exercises(self, db: DbSession, user_id: UUID, exercises: list[PolarExerciseJSON]) -> int:
        """Store each exercise Polar listed that is new, and record it as holding its true time.

        FORK (2.96.6.1). Three rules, the same for the hourly pull and the webhook:

        - An exercise that started more than ``POLAR_WINDOW_DAYS`` ago is not stored (spec Q5, AC3).
        - An exercise already stored for this user (matched on Polar's exercise id) is left exactly
          as it is: no write, no detail, no ``workout.created``. Before 2.96.6.1 the pull re-saved
          every exercise Polar lists (its last 30 days) every hour and re-scheduled its webhook.
          After the offset fix a row stored by the old arithmetic no longer collides on
          (data source, start, end), so without this check every pull would add a second copy.
          Correcting such a row is ``scripts/data_migrations/correct_polar_workout_start_times.py``'s
          job alone (L2), so that it happens once and is counted.
        - A new exercise is stored at its true instant and recorded in ``polar_workout_true_time``
          (``recorded_by = "ingest"``) in the same transaction, which is what keeps the correction
          from ever touching it (AC16).
        """
        oldest_start = datetime.now(timezone.utc) - timedelta(days=POLAR_WINDOW_DAYS)
        count = 0
        for record, detail in self._build_bundles(exercises, user_id):
            if record.start_datetime < oldest_start:
                continue
            if record.external_id is not None and self.workout_repo.get_by_external_id(
                db, user_id, record.external_id, provider=ProviderName.POLAR
            ):
                continue
            created = event_record_service.create(db, record)
            if created.id != record.id:
                # Another record already holds this (data source, start, end): not ours to touch.
                continue
            db.add(PolarWorkoutTrueTime(event_record_id=created.id, recorded_by="ingest"))
            db.commit()
            event_record_service.create_detail(db, detail.model_copy(update={"record_id": created.id}))
            count += 1
        return count

    def get_exercise_detail(
        self,
        db: DbSession,
        user_id: UUID,
        exercise_id: str,
        samples: bool = False,
        zones: bool = False,
        route: bool = False,
    ) -> dict:
        """Get detailed exercise data from Polar API."""
        params = {
            "samples": str(samples).lower(),
            "zones": str(zones).lower(),
            "route": str(route).lower(),
        }
        return self._make_api_request(db, user_id, f"/v3/exercises/{exercise_id}", params=params)
