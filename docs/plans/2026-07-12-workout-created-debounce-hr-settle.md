# Debounce `workout.created` Until HR Settles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Stop `workout.created` from emitting a partial HR trace by deferring the emit via a self-polling Celery debounce that fires once the HR window has stopped growing.

**Architecture:** For workout details, replace the synchronous `after_commit` webhook emit with a scheduled `finalize_workout_zones(workout_id)` Celery task (queue `webhook_sync`). The task polls completeness every `D` seconds and emits once when completeness is stable, ≥ target, or a hard cap `T` is hit — guarded emit-once via Redis `SETNX` + Svix `idempotency_key`.

**Tech:** Python 3, FastAPI, Celery (Redis broker), SQLAlchemy, pytest + Testcontainers. Run tests with `uv run pytest`; lint `uv run ruff check`.

**Spec:** `docs/specs/2026-07-12-workout-created-debounce-hr-settle.md`

## Global Constraints

- Branch `fix/workout-partial-zones-at-ingest` off `release/0.6.2-syn`. Commit LOCALLY only — never push/pull/rebase; PR into the Synaptik fork, Dragan merges/tags. NEVER touch `release/0.6.2-syn` directly.
- **Tunables (CONFIRMED):** `DEBOUNCE_D=1s`, `TARGET_COMPLETENESS=0.95`, `HARD_CAP_T=5s` — in `app/config.py` `Settings`, env-overridable.
- **Emit-once:** Redis `SETNX workout:{id}:emitted` + Svix `idempotency_key=str(workout_id)`.
- **Scope: workout only.** Sleep/menstrual emits stay synchronous (unaffected by this race).
- **Single source of truth:** reuse `event_record_service._compute_workout_hr_zone_fields` (same computation the REST uses). No new zone logic.
- Mirror the existing `app/integrations/celery/tasks/finalize_stale_sleep_task.py` (finalize task + `get_redis_client()`) for style/idioms.
- TDD: pure logic gets unit tests first; Celery/DB wiring gets a focused integration test.

---

## Task 1: Config tunables + the pure settle-decision helper

**Files:**
- Modify: `backend/app/config.py` (`Settings`)
- Create: `backend/app/services/apple/healthkit/workout_settle.py` (pure helper)
- Test: `backend/tests/services/test_workout_settle.py`

**Interfaces (produced):**
```python
# workout_settle.py
def should_emit(
    prev_completeness: float | None,   # completeness read on the previous tick (None on first tick)
    completeness: float | None,        # completeness now (None = no HR series at all)
    elapsed_seconds: float,            # now - first_seen
    target: float,                     # TARGET_COMPLETENESS
    cap_seconds: float,                # HARD_CAP_T
) -> bool
```

- [ ] **Step 1: Add settings.** In `backend/app/config.py` `Settings`, add:
  ```python
  workout_zone_debounce_seconds: int = 1
  workout_zone_target_completeness: float = 0.95
  workout_zone_hard_cap_seconds: int = 5
  ```

- [ ] **Step 2: Write failing tests** — `test_workout_settle.py`:
  ```python
  from app.services.apple.healthkit.workout_settle import should_emit
  T, CAP = 0.95, 5.0

  def test_no_hr_emits_immediately():         # completeness None → nothing to wait for
      assert should_emit(None, None, 0.0, T, CAP) is True
  def test_first_tick_growing_waits():        # prev None, has HR, under target, under cap → wait
      assert should_emit(None, 0.41, 1.0, T, CAP) is False
  def test_stable_emits():                    # completeness unchanged tick-over-tick → settled
      assert should_emit(0.41, 0.41, 2.0, T, CAP) is False  # prev==cur but still under cap AND... see note
      assert should_emit(1.0, 1.0, 3.0, T, CAP) is True     # stable → emit
  def test_grew_waits():
      assert should_emit(0.41, 1.0, 2.0, T, CAP) is False   # grew → keep waiting (unless target)
  def test_target_fast_path():
      assert should_emit(None, 0.96, 1.0, T, CAP) is True    # >= target → emit now
  def test_hard_cap_emits():
      assert should_emit(0.41, 0.80, 6.0, T, CAP) is True    # elapsed >= cap → emit whatever
  ```
  RULE to implement (make the tests above pass exactly): emit iff
  `completeness is None` OR `completeness >= target` OR `elapsed >= cap` OR (`prev is not None` AND `completeness == prev` AND `completeness > 0`). (The `>0` avoids a "stable at 0" early-emit before any HR arrives on a real workout; a genuinely HR-less workout hits the `completeness is None` branch instead.)

- [ ] **Step 3: Run, verify fail** — `uv run pytest backend/tests/services/test_workout_settle.py -q` (module missing).

- [ ] **Step 4: Implement** `workout_settle.should_emit` per the rule.

- [ ] **Step 5: Run, verify pass** + `uv run ruff check backend/app/services/apple/healthkit/workout_settle.py`.

- [ ] **Step 6: Commit** — `feat(workout): settle-decision helper + debounce tunables`

---

## Task 2: `finalize_workout_zones` Celery task

**Files:**
- Create: `backend/app/integrations/celery/tasks/finalize_workout_zones_task.py`
- Test: `backend/tests/integrations/celery/test_finalize_workout_zones.py`

**Interfaces (consumed):** `should_emit` (Task 1); `event_record_service._compute_workout_hr_zone_fields`; `redis_client.get_redis_client`; `settings`.

**Interfaces (produced):**
```python
@shared_task(name="app.integrations.celery.tasks.finalize_workout_zones_task.finalize_workout_zones", queue="webhook_sync")
def finalize_workout_zones(workout_id: str, prev_completeness: float | None = None, first_seen_iso: str | None = None) -> dict
```

- [ ] **Step 1: Write failing tests** (mock the DB fetch + `_compute_workout_hr_zone_fields` + `apply_async` + the emit fn):
  - `emits_and_marks_when_stable`: compute returns completeness 1.0, prev=1.0 → calls the emit path once, sets Redis `SETNX`, does NOT reschedule.
  - `reschedules_when_growing`: prev=None, completeness 0.41 → calls `finalize_workout_zones.apply_async(countdown=1, kwargs prev_completeness=0.41, first_seen_iso=<set>)`, no emit.
  - `noop_when_already_emitted`: Redis guard already set → returns early, no compute, no emit.
  - `noop_when_record_missing`: fetch returns None → no emit.
  - `emits_at_hard_cap`: first_seen 6s ago, still growing → emits.
  - Use `first_seen_iso=None` on the first call → the task stamps `first_seen` = now and passes it forward.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** the task (mirror `finalize_stale_sleep_task.py` for session + redis idioms):
  ```python
  def finalize_workout_zones(workout_id, prev_completeness=None, first_seen_iso=None):
      redis = get_redis_client()
      if redis.get(f"workout:{workout_id}:emitted"):   # already done
          return {"emitted": False, "reason": "already_emitted"}
      with SessionLocal() as db:
          record = db.get(EventRecord, UUID(workout_id))
          if record is None or record.data_source_id is None:
              return {"emitted": False, "reason": "missing_record"}
          data_source = db.get(DataSource, record.data_source_id)
          detail = <fetch persisted workout EventRecordDetail for record.id>
          if data_source is None or detail is None:
              return {"emitted": False, "reason": "missing"}
          zone_minutes, completeness = event_record_service._compute_workout_hr_zone_fields(db, record, data_source)
          now = datetime.now(timezone.utc)
          first_seen = datetime.fromisoformat(first_seen_iso) if first_seen_iso else now
          elapsed = (now - first_seen).total_seconds()
          if should_emit(prev_completeness, completeness, elapsed,
                         settings.workout_zone_target_completeness, settings.workout_zone_hard_cap_seconds):
              # emit-once guard: SETNX; only the winner emits
              if redis.set(f"workout:{workout_id}:emitted", "1", nx=True, ex=3600):
                  _emit_workout_created_from_persisted(db, record, data_source, detail, zone_minutes, completeness)
              return {"emitted": True, "completeness": completeness}
          finalize_workout_zones.apply_async(
              args=[workout_id],
              kwargs={"prev_completeness": completeness, "first_seen_iso": first_seen.isoformat()},
              countdown=settings.workout_zone_debounce_seconds)
          return {"emitted": False, "reason": "rescheduled", "completeness": completeness}
  ```
  Add `_emit_workout_created_from_persisted(...)` in `event_record_service` (Task 3 refactor) OR inline: build the `on_workout_created(...)` call from the persisted `EventRecordDetail` model + `record` + `data_source` + `zone_minutes`/`completeness`, mirroring the `"workout"` branch of `_emit_event_record_webhook` (which currently takes the create-schema). Prefer a shared helper so the payload never drifts (see Task 3).

- [ ] **Step 4: Run, verify pass** + ruff.

- [ ] **Step 5: Commit** — `feat(workout): finalize_workout_zones debounce task (poll → emit-once)`

---

## Task 3: Schedule on ingest + stop the synchronous workout emit + integration test

**Files:**
- Modify: `backend/app/services/event_record_service.py` (`bulk_create_details`, `create_detail`, + a shared `_emit_workout_created_from_persisted` helper)
- Test: `backend/tests/integrations/test_apple_sdk_import.py` (or `tests/api/v1/test_workouts.py`)

- [ ] **Step 1: Extract the workout emit into a model-based helper.** Add `event_record_service._emit_workout_created_from_persisted(db, record, data_source, detail_model, zone_minutes, completeness)` that builds the `on_workout_created(...)` payload from the persisted `EventRecordDetail` (mirror the `"workout"` branch of `_emit_event_record_webhook`, reading `detail_model.energy_burned/distance/heart_rate_avg/...`). The Task-2 task calls this. Keep `_emit_event_record_webhook`'s workout branch delegating to it so there is ONE payload builder.

- [ ] **Step 2: Write failing integration test** — two-upload incident replay:
  - Upload #1: SDK sync with the workout + the first-half HR samples. Assert: **no** `workout.created` emitted synchronously (mock `on_workout_created` / `emit_webhook_event.apply_async` — asserted NOT called during `load_data`), and `finalize_workout_zones.apply_async` **was** scheduled once with `countdown=1` for that workout id.
  - Then invoke the task against a DB that now also has upload #2's second-half HR (insert it, then call `finalize_workout_zones(workout_id, prev_completeness=<first read>, first_seen_iso=<now-2s>)`): assert it emits exactly once with the FULL-trace completeness/zones (not the partial), and sets the Redis guard.
  - Assert a **sleep** upload still emits synchronously (regression: scope is workout-only).

- [ ] **Step 3: Run, verify fail.**

- [ ] **Step 4: Wire the scheduling.** In `bulk_create_details`: when `detail_type == "workout"`, in the `after_commit` closure replace the per-record `_emit_event_record_webhook(...)` call with:
  ```python
  from app.integrations.celery.tasks.finalize_workout_zones_task import finalize_workout_zones
  finalize_workout_zones.apply_async(args=[str(record.id)], countdown=settings.workout_zone_debounce_seconds)
  ```
  (No live zone compute needed at ingest for workouts — the task computes them later. Keep the non-workout branch of `bulk_create_details` and the sleep/menstrual paths of `create_detail` exactly as-is.) In `create_detail`: when `detail_type == "workout"`, schedule the task instead of the synchronous `_emit_event_record_webhook`; else unchanged.

- [ ] **Step 5: Run, verify pass** — the integration test + the full workout/sleep import suites:
  `uv run pytest backend/tests/integrations/test_apple_sdk_import.py backend/tests/api/v1/test_workouts.py -q`. Update any existing test that asserted a synchronous `workout.created` on upload to instead assert the scheduled task (document each). Sleep tests unchanged. `uv run ruff check backend/app`.

- [ ] **Step 6: Commit** — `fix(workout): defer workout.created to the settle debounce (emit-once, full zones)`

---

## Final verification

- [ ] `uv run pytest backend/tests -q` green (note the known local-config needs from `reference_ow_backend_test_runner_setup`: `uv sync --group dev`, gitignored `backend/config/.env`, Docker for Testcontainers).
- [ ] `uv run ruff check backend` clean.
- [ ] Grep: no remaining synchronous `on_workout_created`/`_emit_event_record_webhook` on the workout path at ingest (only the debounce task emits workouts); sleep/menstrual still synchronous.
- [ ] Whole-branch review (SDD final step).
- [ ] PR notes: workout.created now arrives ~1–3s later (debounce) but with the complete HR trace; latency bounded at 5s; REST/reconcile remain the >5s backstop. Sleep/menstrual unchanged. Adapter side: partial-`edwards` rows created before this ships still need a manual reconcile or the optional heal-widening.
