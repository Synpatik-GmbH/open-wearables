# 2.96.6.3 Apple uploads survive a worker restart

Spec v0.1, Product Studio, 20 Oct 2026. Build child of 2.96.6 No lost wearable data. Status: AGREED, ready for build (Andrei, 20 Oct 2026).
Source: read-only check relayed 20 Oct 2026 (OW at `5965a10f`, tag 0.9.0-syn.8): `process_sdk_upload` is acknowledged when a worker thread starts it, so a worker kill loses the Apple Health upload in flight for good; the SDK already received its 202 and never resends. From 16 to 19 Oct worker kills stopped Apple uploads for every member (2.96.6.2).

## 1. Intent

**Who.** Every member who syncs through Apple Health.

**Pain.** When the shared Open Wearables worker is killed for any reason, an Apple upload being processed at that moment is lost for good. For an Apple Watch member that can be a whole night, so V6 breaks.

**Why now.** The founding cohort starts on 27 Oct 2026, and the worker already runs close to its 1 GiB limit (Dragan's #42).

**What this slice delivers.** An Apple upload counts as handled only once it is saved, so an upload cut off by a worker restart is redone, with a cap so one bad upload can never kill the worker again and again.

## 2. Behaviour

- An Apple upload is acknowledged only when its processing completes. If the worker dies first, the upload is redone after the restart (with today's settings, about an hour later).
- A redo creates no duplicate rows (per the check: series are upserts, workouts skip existing rows, sleep is replaced in place).
- After the third delivery of the same upload (C1), it is recorded as failed with a fixed code and is not tried again.
- Nothing else changes: no change to queues, worker layout, Polar pulls or other providers.

## 3. Affected surfaces

- OW fork: `process_sdk_upload` (`process_sdk_upload_task.py`), a per-upload delivery count, the failure code list, tests, FORK-DELTA.
- calibra-ow-deploy: the routine `OW_REF` bump only (Dragan).

## 4. Acceptance criteria

1. An Apple upload whose worker is killed before it completes is processed again after the restart and its data is stored.
2. A redone upload creates no duplicate series rows, workouts or sleep records.
3. The third delivery of the same upload that does not complete records it failed with a fixed code; no fourth attempt is made.
4. An upload that completes on its first delivery behaves as today.

## 5. Invariants

- No new data type; consent gating unchanged; user data stays in EU infrastructure.
- Failure records hold codes, not free text.

## 6. Non-goals

- Running pulls in their own worker (parked until after launch).
- The webhook that can be lost between a batch's commit and its queueing, and workout-zone runs without the same protection (BACKLOG).
- Changing the broker's redelivery timeout.

## 7. Predicates

| # | Predicate | Value | Status |
|---|---|---|---|
| C1 | Delivery cap for one Apple upload | 3 deliveries, then recorded failed with a code | agreed 20 Oct 2026 |

## 8. Data-protection ruling (calibra-dp-desk)

- **Ruling:** covered, no change.
- **Why:** the same Apple Health data under the required consent, now not lost on a restart; the delivery count is operational metadata of ingestion (pattern of R-2026-10-06-a, R-2026-10-13-e).
- **Conditions:** the delivery count holds identifiers and a number only and expires with the upload; failure records hold codes, not free text.
- **Documents affected:** none.
- **DPO:** no need.

## 9. Build decisions (Andrei, 20 Oct 2026)

- C1 counts **consecutive killed deliveries of one attempt**. The count is reset on every exit that is not a kill: success, recorded failure, scheduled retry, early return, raised exception. A count of every start would collide with the five in-task retries of 2.96.6.1.
- The count lives in Redis under `sdk_upload:deliveries:<batch_id>:<retries>`, an integer only, with a 24-hour expiry refreshed on every delivery. A scheduled retry is a new attempt and starts a fresh count; a redelivered message keeps its retries number (Codex P2 on PR #44, decided 20 Oct 2026).
- A refused delivery is recorded with error `sdk_import_failed` and cause `sdk_upload_delivery_cap`. Its count is kept until it expires, so a kill before the refusal is acknowledged cannot reset the cap (Codex P1 on PR #44, decided 20 Oct 2026).
- Known limit, accepted (Codex P1 on PR #44, decided 20 Oct 2026): with `SDK_PAYLOAD_S3_OFFLOAD=true` and `RAW_PAYLOAD_STORAGE=disabled`, a kill between the transport payload's delete and the acknowledgement brings the batch back without its payload, and it is recorded failed although its data is stored. Offload defaults to off and nothing in calibra-ow-deploy turns it on; revisit before it is turned on.
- Worst case: an upload that itself kills the worker kills it three times (deliveries 1 to 3); delivery 4 is refused and recorded failed. Before this change it killed it once and was lost.
