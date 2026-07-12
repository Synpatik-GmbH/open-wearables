# Debounce `workout.created` until the HR trace settles

**Date:** 2026-07-12
**Repo:** open-wearables (Synaptik fork). Branch: `fix/workout-partial-zones-at-ingest` off `release/0.6.2-syn`.
**Status:** Design — pending review (D/T + MVP-vs-poll decisions below).

## 1. Problem

`workout.created` can be emitted with a **partial HR trace**, so the consumer (calibra-adapter) stores a low `hr_trace_completeness` → `signal_quality_tier='Partial'` and an under-counted Edwards load — even though the full HR trace is present in OW moments later.

**Live evidence (Dragan, 2026-07-12, running workout `0e947436`, 46 min):**
- Emitted zones → completeness **0.4133**, zone_4=11, zone_5=0 (webhook payload → adapter).
- OW REST `/events/workouts` NOW → completeness **1.0**, zone_4=35, zone_5=4 (recomputed on-read from the full series).
- The 561 HR `data_point_series` for the workout window landed in **3 batches across ~1.2s** — two uploads:

  | created_at | pts | covers |
  |---|---|---|
  | 18:10:17.792 | 220 | 17:18→17:36 |
  | 18:10:17.836 | 27 | 17:36→17:39 |
  | **18:10:19.008** | **314** | **17:39→18:05** |

  The workout `event_record.created_at = 18:10:17.792` — the **first** batch. `workout.created` fired then, with only the first ~20 min of HR → 0.41. The second upload's HR (the run's high-intensity back half) arrived **~1.2s later**, after the emit.

**Control (same day, walk `17206733`):** its two HR uploads landed **87ms** apart → both present at emit → completeness 0.8375, matches the REST exactly. Same code, no bug — it just won the timing race. This is the natural A/B: **the defect is purely the inter-upload gap** (1.2s tripped it; 87ms didn't).

## 2. What is already fixed (do not redo)

PR #16/#17 fixed the **same-upload** ordering: `import_service.load_data` inserts ALL HR (workout-embedded + the granular statistic-bundle trace) and flushes **before** `event_record_service.bulk_create_details` computes zones + emits (comment in `load_data` documents this). That correctly orders *one* upload's HR before its zone calc.

**The remaining gap is cross-upload late HR:** the workout is emitted from the upload it arrived in; HR for the same window arriving in a *later* upload never triggers a recompute, and `workout.created` never re-fires.

## 3. Constraint

The consumer must NOT flicker (show Partial, then jump to Rich). So the fix must be **emit-once, correct the first time** — i.e. **defer** the emit until the HR window settles, NOT emit-partial-then-re-emit. (Adapter-side re-pull was rejected for exactly this flicker reason; the heal only re-derives `background_hr` rows, so it does not backstop a partial-`edwards` row either — there is currently no safety net.)

## 4. Design — defer `workout.created` via a self-polling Celery debounce

Today the workout webhook is emitted **synchronously** inside the SDK-upload processing (`event_record_service.create_detail` / `bulk_create_details` → `_emit_event_record_webhook`, zones from `_workout_hr_zone_fields` computed from HR present at that instant).

Change: for **workout** details, **do not emit synchronously**. Instead, after the workout row is committed, schedule a Celery task that emits once the HR trace has stopped growing.

### 4.1 The task (new): `finalize_workout_zones`
`app/integrations/celery/tasks/finalize_workout_zones_task.py`, `@shared_task(queue="webhook_sync")`:
```
finalize_workout_zones(workout_id, prev_sampled=None, first_seen_ts=None, attempts=0):
  load record + data_source; if missing/deleted → return (no-op)
  if already emitted (see 4.3) → return
  zones, completeness, sampled_minutes = compute_workout_hr_zone_fields(record)   # existing single-source fn
  now = utcnow(); first_seen_ts = first_seen_ts or now
  settled  = prev_sampled is not None and sampled_minutes == prev_sampled          # HR stopped growing
  enough   = completeness is not None and completeness >= TARGET_COMPLETENESS       # good enough
  capped   = (now - first_seen_ts) >= HARD_CAP_T                                    # bounded — never wait forever
  if settled or enough or capped:
      emit workout.created (zones, completeness) with idempotency_key=workout_id
      mark emitted
  else:
      reschedule finalize_workout_zones.apply_async(
          args=[workout_id], kwargs={prev_sampled: sampled_minutes, first_seen_ts, attempts+1},
          countdown=DEBOUNCE_D)
```
- **Self-polling debounce:** each tick (D apart) re-reads `sampled_minutes` (from `get_workout_hr_zone_minutes`, which already returns it). When it stops growing between two ticks → settled → emit. No external nonce/Redis, no upload↔workout matching — the task reads the live DB each tick.
- **Bounded:** `HARD_CAP_T` guarantees an emit even if HR trickles forever; the REST (on-read recompute) + adapter reconcile remain the backstop for anything beyond T.
- **Emit-once:** `idempotency_key=workout_id` on the Svix event + the emitted-flag (4.3) prevent duplicates from a double-schedule.

### 4.2 Scheduling from `load_data`
After the workout details are committed (`bulk_create_details`), for each **newly inserted** workout, schedule `finalize_workout_zones.apply_async(args=[workout_id], countdown=DEBOUNCE_D)`. Remove the synchronous `_emit_event_record_webhook` call **for the workout detail type only** — `sleep`/`menstrual` keep emitting synchronously (they are not affected by this race). A workout upserted/updated (not newly inserted) that has not yet emitted may also (re)schedule; if already emitted, the task no-ops.

### 4.3 Emitted marker
Add a cheap idempotency guard so the task emits exactly once and a late reschedule can't re-fire:
- Option A (preferred, no migration): rely on the Svix `idempotency_key=workout_id` + a Redis `SETNX workout:{id}:emitted` guard.
- Option B: a nullable `event_record_detail.workout_created_emitted_at timestamptz` column (small migration) set on emit; the task checks it first.

### 4.4 Zones are the single source of truth
`_compute_workout_hr_zone_fields` is already the shared core used by both the REST read and the webhook (comment: "single source — they can never drift"). The task reuses it, so the debounced webhook payload equals what the REST would return at that moment. No new zone logic.

## 5. Tunables (CONFIRMED — Dragan 2026-07-12)

- `DEBOUNCE_D` = **1s** — poll interval / initial defer.
- `TARGET_COMPLETENESS` = **0.95** — emit-now fast path.
- `HARD_CAP_T` = **5s** — max total defer before emitting whatever is present.
All three in settings/config (env-tunable), not hardcoded.

**Trade-off (intentional):** the 5s cap prioritizes low latency. A workout whose HR is still arriving after 5s emits partial and relies on the REST-recompute / adapter-reconcile backstop. The observed incident (second upload +1.2s) resolves well within the cap: tick@1s (partial, prev=None→reschedule), tick@2s (grew→reschedule), tick@3s (stable→emit). The walk (uploads 87ms apart) settles by tick@2s.

**This rules out the "single deferred emit" variant (§9.3):** a single emit at `countdown=1s` would fire *before* the +1.2s second upload → still partial. The self-polling debounce is required at these values.

## 6. Edge cases
- **Workout deleted before finalize:** task no-ops (record gone).
- **No HR at all (background_hr):** `get_workout_hr_zone_minutes` returns None → null zones; emit at first tick (nothing to wait for) so the consumer still gets the workout promptly; the hourly heal path is unchanged for these.
- **Very late HR (beyond T):** emitted at cap with partial zones; REST recompute + adapter reconcile remain the backstop (unchanged). Optionally widen the adapter heal to also re-derive stale-partial `edwards` rows (separate adapter task, noted in the adapter memory).
- **Duplicate schedule (same-window second upload):** both tasks read the same DB; idempotency guard (4.3) ensures one emit.
- **Latency impact:** `workout.created` now arrives ~D later than today (≤ a few seconds in the common case). Acceptable — correctness over a few seconds, and the app already shows the workout from its own HealthKit read; the webhook drives the *derived* load.

## 7. Testing
- **Unit (task):** given a stubbed `get_workout_hr_zone_minutes` whose `sampled_minutes` grows then plateaus across ticks → emits on the tick after it plateaus; emits immediately when completeness ≥ TARGET; emits at HARD_CAP even if still growing; no-op when record missing / already emitted.
- **Integration (`test_apple_sdk_import.py` / `test_workouts.py`):** two-upload scenario reproducing the incident — upload #1 (workout + first-half HR) then upload #2 (second-half HR) within < D → exactly ONE `workout.created`, with full-trace zones/completeness (not the partial from upload #1). Assert no second emit.
- **Regression:** single-upload workout (all HR present) still emits once with correct zones; sleep/menstrual emits unchanged (synchronous).

## 8. Rollout
- Feature branch → PR into Synaptik fork → Dragan merges + tags `0.6.2-syn.5` → deploy via calibra-ow-deploy. Never merge upstream / into `release/0.6.2-syn` directly.
- Config defaults shippable; can dial `DEBOUNCE_D`/`HARD_CAP_T` from data after deploy.

## 9. Decisions (RESOLVED)
1. `DEBOUNCE_D` = 1s, `TARGET_COMPLETENESS` = 0.95, `HARD_CAP_T` = 5s (§5). ✅
2. Emitted-marker: **Redis `SETNX workout:{id}:emitted`** (no migration; Svix idempotency_key=workout_id as the second guard). ✅
3. **Self-polling debounce (§4.1)** — required at D=1s (the single-deferred variant would miss the +1.2s upload). ✅
