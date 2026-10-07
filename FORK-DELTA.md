# Synaptik Fork Delta & Upgrade Recipe

This fork of [the-momentum/open-wearables](https://github.com/the-momentum/open-wearables)
carries a small, deliberate set of Synaptik customizations on top of a pinned upstream
release. **This file lives ONLY on the `release/*-syn` branch** — never on `main`, because
`main` is a pristine upstream mirror that gets fast-forwarded (overwritten) on every sync.

## Branch model

```
the-momentum (upstream)  ──tag──►  main            (pristine mirror, fast-forward ONLY, 0 syn commits)
                                     │  merge per upgrade
                                     ▼
                           release/x.y-syn          (this branch = main + the delta below)
                                     │  tag  x.y-syn.N
                                     ▼
                     calibra-ow-deploy (OW_REF = tag)   ← production
```

- Production is built by `calibra-ow-deploy/.github/workflows/deploy-openwearables.yml`
  from `OW_REPO = Synpatik-GmbH/open-wearables` at the tag in `OW_REF` (`0.6.2-syn.10` as of 2026-10-05; the first tag on this branch will be `0.9.0-syn.1`).
- Deploys always reference an **immutable tag**, never a branch head.

## Current base

- **Now on: 0.9.0** (`ff8527a`) — merged from 0.6.2 (`a07818f`) on 2026-10-05, merge commit `c61c550`.
- `release/0.9.0-syn` = the 0.9.0 mirror plus the delta below. **Not tagged and not deployed yet.**
- `release/0.6.2-syn` (latest tag `0.6.2-syn.10`) is what dev and prod run until `OW_REF` moves.

---

## 0.9.0 upgrade — actual outcome (2026-10-05)

Merge of upstream 0.9.0 (194 commits past 0.6.2) into the 0.6.2-syn delta. 22 files conflicted.
**Backend suite: 2686 passed, 2 skipped. `ruff check`, `ruff format --check`, `ty check` and
`scripts/check_migrations.py` pass.**

**Closed by the bump:**
- **Garmin webhook accepted any caller (2.71.2)** — upstream `7e07f4f` (#1507) compares the
  `garmin-client-id` header to `settings.garmin_client_id` and **fails closed when it is unset**.
  → **DEPLOY ACTION: set `GARMIN_CLIENT_ID`** wherever Garmin is to be received; without it every
  Garmin webhook is rejected. Garmin is not live in either environment.

**⚠️ DEPLOY ACTION that decides whether anything is delivered — `OUTGOING_WEBHOOKS_ENABLED=true`:**
- Upstream `8dcb85a` (#1293) added `outgoing_webhooks_enabled` (default **false**, `config.py`) and
  `_build_client()` in `services/outgoing_webhooks/svix.py` now returns no client unless it is
  true. At 0.6.2 the client was built whenever a Svix token resolved, which is all both
  environments configure. Every fork webhook path keys on `svix_service.is_enabled()`: the
  `finalize_workout_zones` scheduling in `create_detail` / `bulk_create_details`, `_dispatch`,
  and `emit_webhook_event`. **With the flag unset a 0.9.0 deployment sends no webhook at all** —
  no `workout.created`, no sleep, no series — and nothing fails: the app boots, the suite is
  green (the dispatch tests enable Svix themselves), and the adapter simply stops hearing from OW.
- `calibra-ow-deploy` does not set it and, checked 2026-10-05, none of the four dev apps
  (`aca-ow-api-dev`, `aca-ow-worker-dev`, `aca-ow-worker-priority-dev`, `aca-ow-beat-dev`) has it.
  **Set it on all four in each environment before `0.9.0-syn.*` goes there**, in the deploy
  workflow and `bootstrap.sh`, not by hand. `scripts/init/create_svix_db.py` skips on the same
  flag.
- Found by an independent review of the merge, not by the suite or the dev-copy comparison:
  the comparison called REST routes and never emitted a webhook.

**Re-checked, still ours (upstream has not fixed them):**
- **authorize requires a credential** (2.41.8) — upstream `authorize_provider` still has no auth
  dependency at 0.9.0.
- **SDK auth rejects a token whose user is gone** (2.41.9.1) — upstream `get_sdk_auth` still
  trusts `sub`. Upstream's new `get_combined_auth` (#1601) delegates to `get_sdk_auth`, so the
  delta's lookup covers that path too.

**Resolutions that were decisions, not mechanics:**
- **Migrations — a merge revision, not a re-point.** Fork `7c3e9a41d2b8` and upstream
  `7d6921a86914` both descend from `9f0940493a9b`. Dev and prod already record `7c3e9a41d2b8`, so
  re-pointing it behind upstream's head would make them treat upstream's six new migrations as
  applied and skip them (upstream's own `backend/migrations/README` describes the trap). Added
  `e4b7a1c9d3f2_merge_upstream_0_9_0` with `down_revision = ("a7c3e9f1b2d4", "7c3e9a41d2b8")`.
  **Every later bump needs another merge revision** for the same reason: join upstream's new head
  with the fork's current head, never re-point either.
- **`scripts/start/worker.sh`** — upstream now starts two workers per container, an I/O worker and
  a CPU worker for a new `xml_sync` queue. That is the default. With `CELERY_QUEUES` set (the
  fast-lane container) the script starts only that one dedicated worker, as before, so the
  fast-lane container never picks up `xml_sync`. The general worker container now also runs a
  two-process prefork worker; watch its memory (0.5 CPU / 1 GiB).
- **`scripts/init/create_svix_db.py`** — kept the fork's existence check and loud failure, inside
  upstream's `outgoing_webhooks_enabled` gate, instead of upstream's warn-and-continue.
- **`services/apple/healthkit` → `services/sdk`** (upstream rename) — `workout_settle.py` follows;
  `import_service.py` carried the HR-before-details ordering across the rename.
- **`docs/data-migrations/` → one page `docs/dev-guides/data-migrations.mdx`** (upstream) — the
  Svix pseudonymisation page moved to `docs/dev-guides/pseudonymise-svix-user-channels.mdx`.

**Ported by hand (no conflict marker):**
- **Workout pace** — upstream derives `avg_pace_sec_per_km` from distance and time
  (`pace_sec_per_km`), because `average_speed` is km/h on Suunto and m/s elsewhere. The fork's
  deferred emit, `_emit_workout_created_from_persisted`, still used `1000 / average_speed` after
  the merge. It now uses the upstream helper. Pinned by
  `test_workout_webhook_pace_comes_from_distance_not_average_speed`, rewritten to drive the
  deferred path. The adapter does not read this field.

**Tests adjusted for upstream behaviour:** API keys are stored hashed (tests send
`ApiKeyFactory().plain_key`); `_dispatch` drops events while Svix is disabled (dispatch tests
enable it); structured log lines carry a `timestamp`; upstream's authorize tests send a
credential; the readable-channel sweep exempts the Withings OAuth scope literal.

**Verified on a copy of the dev database** (dump restored locally at `7c3e9a41d2b8`, then
`alembic upgrade head`):
- All seven revisions ran. 18 of 21 tables byte-identical; the other three changed as designed
  (`api_key` hashed, `event_record_detail` dropped, `health_score.sleep_record_id` renamed with
  all links intact). No orphaned detail rows. The existing API key still authenticates.
- 253 REST requests across every dev user, old code on the untouched copy against new code on
  the upgraded one: same status codes, no field removed, HR-zone fields identical on all 603
  workout rows (only 4 of those carried non-null zones in the 14-day slice).
- **Existing detail rows lose their original `created_at`**: it lived on `event_record_detail`,
  and the three detail tables get a new `created_at` defaulting to the migration's run time.
  Nothing in the fork or the adapter reads it.

**Adapter-facing change found by that comparison — DEPLOY ORDER:**
- On REST, `source.provider` used to carry the writer (the HealthKit source name) and now carries
  the integration (`apple`); the writer moved to `source.source`. The adapter groups step samples
  by the writer, so an adapter without the fix **over-counts Apple users' steps** (1.38x and 1.58x
  measured). Fixed in calibra-adapter #368 (merged 2026-10-05, `36245bb`), which reads the new
  member and falls back to the old one. **The adapter with #368 must be running in an environment
  before `0.9.0-syn.*` is deployed there.** See "Upstream behaviour the adapter depends on".
- `sleep_stage_intervals` on `/events/sleep` is now returned only with `include`; the adapter
  does not read it.

**Rollback is no longer a tag flip.** After `0.9.0-syn.*` migrates a database, the 0.6.2 code
cannot run on it (`api_key.id` is a UUID, `event_record_detail` is gone), and the downgrade of
`a7c3e9f1b2d4` deletes every API key. **Snapshot the database before the first 0.9.0 deploy in
each environment**; rolling back means restoring it and reverting `OW_REF`.

**Decided 2026-10-05 — `user_id` stays out of the logs SDK log ingestion reaches (`a25099b`).** At
0.9.0 the SDK-logs handler calls `emit_sync_started(user_id, …)` when a batch carries
`syncSessionId` and a `HISTORICAL_SYNC_START` event (`api/routes/v1/sdk_logs.py`), and
`sync_status_service.emit` wrote a `Sync status` line with `user_id`. The fork now removes it;
see the 🔒 row "`user_id` out of the `Sync status` line". Disclosing it was rejected: the DPIA
states that SDK log ingestion holds no personal data.

**New processing at 0.9.0 the DPIA does not describe yet (erasable, so not a broken control):**
the same call stores the event in Redis under `sync:status:user:<user_id>:*` for 24 hours
(`HISTORY_TTL_SECONDS`) and, for a historical run, a `sync_run` row (new table, `user_id`
`ON DELETE CASCADE`) with per-type outcomes in `sync_run_data_type`. It also sends a
`sync.started` webhook through Svix. None of this existed for this endpoint at 0.6.2.

**Not done here:** `/sync/recent` was not compared (needs Redis); migration timing on the full
780 MB time-series table was not measured; no webhook delivery was replayed end to end.

---

## 0.6.2 upgrade — actual outcome (2026-07-07)

Reconcile of the 0.5.2-syn delta onto upstream 0.6.2. **CI green: 1873 passed, 2 skipped.**

**Dropped as superseded by upstream:**
- **session-lifecycle hardening** (last_synced_at / whoop / data_247) — upstream now reads `last_synced_at` *before* the commit + added its own rollbacks and `pull_inserted`/`pull_updated` accounting (our fresh-session rewrite would have broken that accounting).
- **event_record snapshot-before-after_commit** — upstream #1208 fires the webhook directly, no snapshot dataclasses.
- **Polar TL/distance float** — upstream #1204 does the same widening.

**Reconciled / kept:**
- **config** — adopted upstream's `redis_ssl` field; **kept `ssl_cert_reqs=none`** for the Azure Redis Enterprise endpoint (`redis-ow-nc-dev-gwc`, Balanced_B0). It's the proven-working setting; `required` would *likely* also work (Enterprise uses a public DigiCert G2 cert on the FQDN) but isn't worth the cutover risk. Kept WHOOP `read:profile`. Updated the two upstream `test_redis_url` assertions to expect `none`. → **DEPLOY ACTION: rename env `REDIS_USE_TLS` → `REDIS_SSL`** (value stays `true`); the connection string is otherwise identical.
- **Edwards HR-zone payload** — reworked onto upstream's direct-fire model (**no snapshot dataclasses**). `heart_rate.py` + the `get_workout_hr_zone_minutes` SQL recovered verbatim; zones computed while the session is live and passed through `create_detail` → `_emit_event_record_webhook` → `on_workout_created` (bulk path computes before expunge). Emitted payload is identical to 0.5.2-syn → **.NET adapter parity preserved**.
- **respiratory_rate** + **Polar RHR bridge** — kept; declared the newly-emitted series types in upstream 0.6.2's coverage manifests (`whoop/coverage.py` → `respiratory_rate`, `polar/coverage.py` → `resting_heart_rate`) to satisfy the new `test_provider_coverage`.
- **webhook fast lane** — applied clean.

---

## The durable delta (what must survive every upgrade)

These are genuinely Synaptik-specific and will never be upstreamed. **The reconciliation
checklist for any upgrade is: confirm each of these still applies and still behaves.**

Commit hashes below are the shas on `release/0.6.2-syn`; upgrades since 0.6.2 are merges, so they are still the commits in this branch's history.
A row that gives a PR number cites that PR's **merge** commit; a row without one cites the fix
commit itself.

🔒 marks a **data-protection control** — something the DPIA states as operating. Its row says what
breaks if it is dropped, because the consequence is not "a feature is missing": the deployment stops
doing what it is documented to do, and nothing fails to tell you.

> ⚠️ **`backend/app/config.py` is the likeliest conflict point, and the place a silent drop is
> least visible.** Six of the deltas below put a hunk in it: WHOOP `read:profile` + Redis
> `ssl_cert_reqs=none` (`565ae6a`), the fast-lane priority event set (`9519029` — whose row does
> not list `config.py` at all), `svix_payload_retention_days` (`e70564f`), `svix_pseudonym_secret`
> and its derivation validator (`4a7db35`), `sdk_refresh_grace_seconds` and its validator
> (`b8debf6`), and the three `workout_zone_*` debounce tunables (`67227bb`). Settling a
> `config.py` conflict by taking upstream's file wholesale drops all six in one move and
> **nothing complains** — the app starts and the suite passes. The losses surface only in
> production: 90-day Svix payload retention, readable user ids on every Svix message, partial
> HR zones on `workout.created`, and phones locked out by a lost rotation reply. After every
> merge, diff `backend/app/config.py` against `main` and reconcile hunk by hunk rather than
> trusting the merge result.

| Feature | Commit | Files | Why it's ours | Notes |
|---|---|---|---|---|
| **Edwards HR-zone payload** on `workout.created` | `15056f2` | `event_record_service.py`, `heart_rate.py`, `data_point_series_repository.py`, `outgoing_webhooks/events.py` | **The .NET adapter depends on this** — its `WorkoutHrZoneHealService` replicates the EXACT Edwards algorithm. Dropping/changing it breaks parity. | ⚠️ HIGHEST-RISK. Delivery is **deferred**, not direct-fire: since `67227bb` (#18) `create_detail` / `bulk_create_details` schedule a `finalize_workout_zones` settle-debounce task after commit and there is **no synchronous emit at ingest** for workouts (the `workout` branches of `create_detail` and `bulk_create_details`); only sleep / menstrual still emit synchronously. Zones are computed by that task from the settled trace, not while the ingest session is live. Re-verify payload shape against the adapter after any upstream zone change. |
| **Edwards HR-zone minutes on REST `/events/workouts`** | `862a29c` | `event_record_service.py` (`_compute_workout_hr_zone_fields`, `get_workouts`), `schemas/responses/activity/events.py` | REST counterpart of the webhook delta above. Upstream `Workout` carries no zone fields, so the adapter's **reconcile** pull got no zones → `background_hr` → downgraded an already-`edwards` row. Computes zones on read via the same `get_workout_hr_zone_minutes`. | Same `hr_zone_N_min` + `hr_trace_completeness` field names as the webhook payload (single source of truth). Compute-on-read (no persistence), so historical workouts self-heal. |
| **`workout.created` deferred to a settle debounce** | `67227bb` (#18) | `services/event_record_service.py`, `integrations/celery/tasks/finalize_workout_zones_task.py` (new), `integrations/celery/tasks/__init__.py`, `services/sdk/workout_settle.py` (new; `services/apple/healthkit/` until upstream renamed the package at 0.9.0), `config.py` (`workout_zone_debounce_seconds`, `workout_zone_target_completeness`, `workout_zone_hard_cap_seconds`), `tests/conftest.py`, `tests/integrations/celery/test_finalize_workout_zones.py`, `tests/services/test_workout_settle.py`, `tests/services/test_event_record_service.py`, `tests/integrations/test_sdk_import.py`; `docs/specs/` + `docs/plans/2026-07-12-workout-created-debounce-hr-settle.md` | A workout's HR trace does not all arrive in the upload that creates the workout, so zones computed at ingest are partial and upstream's single direct-fire `workout.created` shipped whatever happened to be there. `create_detail` / `bulk_create_details` now schedule one `finalize_workout_zones` task per workout after commit; it re-polls trace completeness and emits **once** — at `workout_zone_target_completeness` (0.95) or when `workout_zone_hard_cap_seconds` (5s) elapses. Sleep / menstrual keep the synchronous after-commit emit. | ⚠️ **This is the delivery mechanism of the HIGHEST-RISK Edwards row above**, so any upstream change to after-commit webhook dispatch conflicts here first. `tests/conftest.py` autouse-mocks `finalize_workout_zones.apply_async` — an upstream `conftest.py` conflict that loses that fixture makes unrelated suites enqueue real Celery work. Needs Redis (settle marker) and a consumer of the **`webhook_sync`** queue — the task is declared `queue="webhook_sync"` (`finalize_workout_zones_task.py`), so the settle polls ride the fast lane. Both the general worker's default list and the priority worker consume it; dropping `webhook_sync` from the general worker would move every settle poll onto the priority container. Emit-once is the contract: two emits is a worse regression than a late one. |
| **HealthKit inserts the HR trace before workout details** | `a438b10` (#16, `6f4e4ed`) | `services/sdk/import_service.py`, `tests/integrations/test_sdk_import.py` (both renamed by upstream at 0.9.0) (+ a `ty` silencing in `services/personal_record_service.py`, `ce5da51`) | `load_data` created workout details — which compute the Edwards zones for `workout.created` from the `heart_rate` series **present in the DB** — before inserting the same upload's HR trace (the records section). A workout whose per-minute HR shipped in the SAME HealthKit upload still emitted null `hr_zone_*_min`, and the adapter showed the lower background-HR load until an hourly heal re-derived Edwards. All HR samples (workout-embedded + records trace) are now inserted and flushed before `bulk_create_details`. | Re-applies an ordering guarantee **already lost once to an upgrade** (0.5.2-syn `29c0a35`, dropped by the 0.6.1 direct-fire rework) — check it explicitly every time. Regression test: a workout co-uploaded with its per-minute HR trace must emit non-null `hr_zone_3_min`. The `#18` debounce reduces but does not remove the need for the ordering — the settle task recomputes zones, but the completeness signal it polls is this same trace. |
| **Webhook fast lane** | `9519029` | `outgoing_webhooks/events.py`, `scripts/start/worker.sh` | Latency: priority events → dedicated `webhook_sync` Celery queue (`CELERY_QUEUES`). Consumed by `aca-ow-worker-priority-dev`. | Keep the `worker.sh` CELERY_QUEUES override. Since 0.9.0 upstream starts an I/O and a CPU (`xml_sync`) worker; with `CELERY_QUEUES` set the script `exec`s one dedicated worker for those queues and starts no CPU worker. No test covers the script — after any merge, read it. |
| **Polar Recharge bridges** | `738186b` | `services/providers/polar/data_247.py`, `polar/coverage.py` | HRV→`rmssd`, RHR→`resting_heart_rate`. Not in upstream. | Must stay declared in `polar/coverage.py` (test_provider_coverage). |
| **respiratory_rate → data_point_series** (Thread 14f) | `1d2f46a` | `data_point_series_repository.py`, `whoop/coverage.py`, sleep save path | Sleep-side RR persistence. | Declared in `whoop/coverage.py`. Upstream `eddf5d9` #1235 is Garmin-side RR (complementary). |
| **WHOOP `read:profile` scope + Redis `ssl_cert_reqs=none`** | `565ae6a` | `config.py`, `tests/utils_tests/test_redis_url.py` | WHOOP app needs the scope; Azure Redis Enterprise proven with `none`. | Uses upstream's `redis_ssl` field → deploy env is **`REDIS_SSL`**. Two `test_redis_url` assertions carry the `none` delta. |
| **`GET /oauth/{provider}/authorize` requires a credential** (2.41.8) | `23d2ce2` | `api/routes/v1/oauth.py`, `tests/api/v1/test_oauth.py`; docs half at `10e66aa` — `docs/api-reference/guides/error-handling.mdx`, `docs/dev-guides/how-to-add-new-provider.mdx` | Upstream leaves it unauthenticated: any caller could mint OAuth state for any `user_id` and bind their own wearable to that user, or plant a `redirect_uri` the callback follows. `ApiKeyDep` (API key or developer JWT) closes it. The .NET adapter already sends `X-Open-Wearables-API-Key` on this call. | Upstream's own docs (`quick-integration.mdx`, `integration-guide.mdx`) already send the API key on this call. On each upgrade, check whether upstream's `authorize_provider` now carries an auth dependency; if it does, drop this delta (re-checked at 0.9.0: it does not). **Breaks the frontend `/users/$userId/pair` page**, which calls authorize with no credential; deliberate, the frontend is not deployed. |
| **SDK auth rejects a token whose user no longer exists** (2.41.9.1) | `004b009` | `utils/auth.py` (`get_sdk_auth`, `_parse_sdk_subject`), `tests/utils_tests/test_sdk_auth.py`, `tests/api/v1/test_sdk_sync_auth.py`, `tests/api/v1/test_sdk_logs.py`, `tests/services/test_sleep_service.py`; docs note in `docs/sdk/{ios,android,flutter,react-native}/integration.mdx`, `docs/sdk/index.mdx`, `docs/dev-guides/integration-guide.mdx` | Upstream decodes the SDK JWT and trusts `sub`, so a deleted user's token keeps getting 202 for the 60-minute access-token lifetime. A decodable SDK token is now a credential only while its user row exists; an API key alongside cannot rescue it; a lookup failure propagates. | On each upgrade, check whether upstream's `get_sdk_auth` gains a subject lookup; if it does, drop this delta (re-checked at 0.9.0: it does not; upstream's new `get_combined_auth` delegates to `get_sdk_auth`, so the lookup covers it). Tests that mint SDK tokens must create the user first. |
| 🔒 **Svix never receives a readable identifier** — hashed `eventId`, pseudonymous user channels | `4a7db35` (#22) | `services/outgoing_webhooks/pseudonyms.py` (new), `services/outgoing_webhooks/svix.py`, `services/outgoing_webhooks/events.py`, `api/routes/v1/outgoing_webhooks.py`, `config.py` (`svix_pseudonym_secret` + `derive_svix_pseudonym_secret`), `config/.env.example`, `scripts/data_migrations/pseudonymise_svix_user_channels.py` (new), `tests/services/test_svix_pseudonyms.py`, `tests/scripts/test_pseudonymise_svix_user_channels.py`, `tests/api/v1/test_outgoing_webhooks.py`; `docs/api-reference/guides/webhooks.mdx`, `docs/dev-guides/pseudonymise-svix-user-channels.mdx`, `docs/dev-guides/data-migrations.mdx`, `docs/docs.json` (upstream folded `docs/data-migrations/` into `docs/dev-guides/` at 0.9.0) | Upstream sends a readable `eventId` (user id + provider + metric + ingestion window) and a `user.<uuid>` channel on every message. svix-server v1.69.0 keeps `message.uid` and `message.channels` on the message row **with no expiry** — payload retention reaches only `messagecontent`, there is no config key and no API operation, and the prune subcommand that would remove them only exists from a much later version. So every emitted event wrote a permanent copy of the user id into Svix. `eventId` is now an HMAC-SHA256 hex digest (input string unchanged, so dedup is unchanged); user channels are `user.` + AES-SIV of the user id, hex (deterministic, so endpoint filtering still matches; reversible with the key, so the endpoints API still reports `user_id`). **Survival: this is the control that makes Svix erasable at all.** Lose it and every webhook resumes writing a readable user id into a store with no deletion path — an Art. 17 erasure request cannot be satisfied for anything already delivered, and the DPIA statement that the webhook bus carries no readable identifier is false from the first event after the bump. That is a change of processing, not a lost feature. | `send()` pseudonymises at the **Svix boundary**, not only at the nine producers, so a Celery job enqueued by the previous release during a rolling deploy cannot smuggle a readable channel through. `migrate_legacy_user_channels()` is a **one-shot operator script**, not startup work — run `scripts/data_migrations/pseudonymise_svix_user_channels.py` once after upgrading; idempotent, exit 1 when Svix is unconfigured or a request failed (just rerun). Existing Svix rows are deliberately not rewritten. `SVIX_PSEUDONYM_SECRET` when unset is **derived** from `secret_key` under a fixed context, never equal to it (digests are returned to API callers and must not be HMAC outputs of the key that signs access tokens); rotating it changes every pseudonym — events already in Svix stop deduplicating against new ones, and a user-filtered endpoint stops receiving until its `user_id` is saved again (and then only after Svix endpoint cache refresh). `test_no_module_builds_a_readable_user_channel` sweeps `app/` for any `user.` literal or `USER_CHANNEL_PREFIX` use outside `pseudonyms.py` — it is what catches an upstream merge reintroducing a raw channel. |
| 🔒 **5-day Svix payload retention + endpoint-less-developer skip** | `e70564f` (#21) | `config.py` (`svix_payload_retention_days`, `Field(ge=5, le=90)`), `services/outgoing_webhooks/svix.py`, `integrations/celery/tasks/emit_webhook_event_task.py`, `config/.env.example`, `tests/test_config_svix_payload_retention.py`, `tests/api/v1/test_outgoing_webhooks.py`; `docs/api-reference/guides/webhooks.mdx`, `docs/api-reference/guides/sync-status-stream.mdx` | svix-server has no server-level retention setting, so retention is set **per message at creation**, and upstream sets none — which means the SDK default of **90 days**, confirmed against live data in both environments. Every payload (full sleep, workout and HR series for one identified user) sat in Svix for 90 days. 5 days is now set explicitly: the platform floor, still far clear of the 27h 35m retry tail (`[5, 300, 1800, 7200, 18000, 36000, 36000]`s) and well inside the one-month erasure response window. The emit task also looped **every** developer account, storing one copy of each payload per developer whether or not that account could receive it; it now emits only to developers with at least one registered endpoint, and no longer creates a Svix application for an account that never registered one. **Survival: the regression is completely silent.** Drop the per-message `payloadRetentionPeriod` in a merge and Svix accepts the message and quietly applies its own 90-day default — no error, no failing test, and the storage-limitation period the DPIA records (5 days) becomes 90. The per-developer fan-out is the same shape: extra copies of health data, nothing fails. Both are Art. 5(1)(e) commitments, not tuning. | `payloadRetentionHours` does **not** work: svix-server accepts it with a 202 and then ignores it (an earlier revision of this branch used 48h and messages still expired at 90 days). Only `payloadRetentionPeriod` is honoured, on both v1.69.0 and 1.101.0, and only for 5—90 days — hence `Field(ge=5, le=90)`, so an out-of-range deploy fails configuration validation at startup instead of 422-ing every webhook at runtime. Also in this PR and easy to lose separately: an **unreachable** Svix no longer counts as no-endpoint (`has_endpoints` returned `False` on `ConnectError`, so the task acked and the event was dropped with no send and no retry). Only an answer that *proves* there is no endpoint — an empty list, or a 404 for an application never created — may skip; every lookup that merely failed returns `True` and defers to `send()`, which owns the delivery-failure contract. Every swallowed lookup failure, `ConnectError` included, goes through `log_and_capture_error` → Sentry (`783635e`), as `backend/AGENTS.md` requires for handled exceptions in background paths. |
| 🔒 **SDK refresh-token rotation survives a lost reply** | `b8debf6` (#20) | `services/refresh_token_service.py`, `repositories/refresh_token_repository.py`, `models/refresh_token.py` (`rotated_from`), `api/routes/v1/sdk_token.py`, `config.py` (`sdk_refresh_grace_seconds` + `_validate_sdk_refresh_grace_seconds`), `migrations/versions/2026_09_11_1200-7c3e9a41d2b8_refresh_token_rotated_from.py`, `tests/services/test_refresh_token_{service,logging}.py`, `tests/repositories/test_refresh_token_repository.py`, `tests/integrations/test_sdk_refresh_concurrency.py`, `tests/integrations/sdk_refresh_race_support.py`, `tests/api/v1/test_token.py`, `tests/test_config_sdk_refresh_grace.py`; `docs/sdk/{ios,android,flutter,react-native}/integration.mdx`, `docs/superpowers/specs/2026-09-11-sdk-refresh-token-lost-rotation-design.md`, `docs/superpowers/plans/2026-09-11-sdk-refresh-token-lost-rotation.md`, `docs/superpowers/specs/queries/2026-09-11-sdk-refresh-stuck-clients.kql` | Upstream revokes the presented refresh token the instant rotation succeeds. If the reply never reaches the phone (dropped connection, backgrounded app), the successor it never saw is the only live token and **every later refresh is 401, permanently** — the only way out is reconnecting by hand, and nothing in the app says so. A superseded token whose successor exists and is still unrevoked now returns that same successor, within `sdk_refresh_grace_seconds` (default 7 days); grace writes no rows and is idempotent. Rotation is serialised per `(user, app)` with a `pg_advisory_xact_lock` and successors are linked through `rotated_from`. **Survival: a locked-out phone keeps recording and stops delivering, so nothing alerts.** The user is never told, no error surfaces, and the health record OW holds for that person is silently incomplete for the whole period — an accuracy and availability failure (Art. 5(1)(d), Art. 32) the DPIA counts as a control, not a UX nicety. Detection depends on it too: the stuck-client KQL query only works because every refresh outcome is logged through the D-08 constant, so a merge that drops the logging also blinds the alarm. | Adds a column, so the Alembic revision travels with the delta (`7c3e9a41d2b8`, down `9f0940493a9b`). **Never re-point it**: deployed databases record it, so each upgrade joins it to upstream's head with a merge revision instead (`e4b7a1c9d3f2` at 0.9.0). Also here and separable by accident: a fresh SDK mint revokes the earlier live tokens for that `(user, app)` (Calibra is one phone per account) and those revocations write **no** `rotated_from`, so a mint-revoked token is final and never handed back through grace; revoking a superseded token also revokes its unused successor, so logging out after a missed rotation cannot leave a live token reachable. Developer tokens are untouched on every path (D-04) and the invitation-code mint path is out of scope. All five DB operations sit in `RefreshTokenRepository` (`backend/AGENTS.md:297`) and **none of them commit** — pushing a commit into any one would release the advisory lock early. On each upgrade, check whether upstream rotation grew a grace window of its own. |
| 🔒 **`user_id` out of the SDK-logs summary line** | `2f2112b` (#25) | `api/routes/v1/sdk_logs.py` | `submit_sdk_logs` discards the body — `store_raw_payload` returns immediately while raw-payload storage is disabled, and nothing is persisted or forwarded — so its structured log line is the **only durable trace** of the endpoint, and `user_id` was the only personal datum in it. `batch_id`, `provider`, `event_count`, `event_types` and `sdk_version` are kept: no personal data, and they are what makes the line useful. Correlate on `batch_id`. **Survival: the observability window is a separate retention regime with no erasure path.** A deletion request reaches the database, not 30 days of log storage; once a user id is written there it stays for the full window whatever happens to the user row. One line of upstream reformatting puts the field back, nothing fails, and the DPIA statement that SDK log ingestion holds no personal data quietly stops being true. | This removes one copy, **not both**. The user id is also in the URL path, and the request log still writes the path. **How it gets there changed at 0.9.0:** upstream disables `uvicorn.access` (`main.py:51`) and logs requests itself through `add_access_log_middleware` (`middlewares.py`), one `http_request` line carrying `path` (query values kept, except the keys in `_REDACTED_QUERY_KEYS`). The level comes from `access_log_level`, which with `ENVIRONMENT=production` — what `calibra-ow-deploy` sets on every app — derives to **errors only**, so from 0.9.0 the path is logged for 4xx/5xx responses and no longer for every request. Two things to keep off: `LOG_ERROR_RESPONSE_BODY` (default false) would add the 4xx response body to that line, and setting `ACCESS_LOG_LEVEL=all` would bring back a path line per request. A 5xx line also carries the exception message and traceback. That copy is **accepted and disclosed**, not fixed: suppressing paths in the global access log would cost 5xx debugging across the whole API to protect one route. Keep the comment above the `log_structured` call — no test pins the absence, so the comment is the only thing standing between this delta and someone re-adding `user_id` as an obvious improvement. |
| 🔒 **`user_id` out of the `Sync status` line** (and the two failure lines beside it) | `a25099b` | `services/sync_status_service.py`, `tests/api/v1/test_sdk_logs_keep_user_id_out_of_logs.py` | From 0.9.0 SDK log ingestion opens a sync run, so `emit` runs for that endpoint. Three lines there could carry the user id into a log window with no erasure path: `Sync status` (the field), `Failed to persist sync run` (the field, and a database error's text, which quotes the statement parameters) and `Failed to emit sync status event` (a Redis pipeline error's text and traceback, which quote the key and payload). The field is gone and both failure lines log the error's type only. It applies to every sync run, not only this endpoint. Correlate on `run_id`: a stored run maps to its user in `sync_run` and stops mapping when the user is deleted. | Upstream owns this file and will touch these calls again; re-adding `user_id` reads as an obvious improvement. The tests post through the endpoint and search everything written to stdout, stderr and the logging tree for the user id, so they hold whatever the line is called. They set the `http_request` line aside by name — that is the accepted copy in the row above. If upstream adds a new log call on this path, the tests see it only on the three paths they drive (normal, run write fails, Redis write fails). |
| 🔒 **A failed sync keeps an error code, never the error's text** (Notion 2.47.17.2) | `1073ef67` | `services/sync_error_code.py` (new), `services/sync_error_cleanup.py` (new), `services/sync_status_service.py` (`emit`, `try_record_data_types`), `integrations/celery/tasks/sync_vendor_data_task.py`, `process_sdk_upload_task.py`, `close_stale_sync_runs_task.py`, `repositories/sync_run_repository.py` (two reads, two locked rewrites), `services/sdk/import_service.py`, `schemas/webhooks/event_types.py`, `docs/api-reference/guides/sync-status-stream.mdx`, `scripts/data_migrations/reduce_sync_run_errors_to_codes.py` (new), `scripts/start/app.sh`; `tests/services/test_sync_error_is_a_code.py`, `tests/scripts/test_reduce_sync_run_errors_to_codes.py` | An exception's text is not bounded: a database error quotes the statement's parameters, which are the health values being saved and the user id. A sync event's `error` was that text, and it is stored (`sync_run.error`, `sync_run.meta`, `sync_run_data_type.error` / `error_code`: new in 0.9.0), kept in the Redis sync history (a run's last event for 24 hours; a user's recent list for as long as new events keep renewing it, up to its last 200), written to the `Sync status` log line and sent as the `sync.failed` webhook. `emit` reduces `error`, and whatever sits under an `error` key in `metadata`, to a code before any of those runs: the name of an exception class from a fixed registry (with the HTTP status when it has one) or one of the fixed words in `sync_error_code.py`, else `unclassified`. The registry is the exception classes that a listed set of modules export (`_APP_MODULES`, `_LIBRARY_MODULES`), so it is the same in the worker that stores a code and the start-up script that reads it back. Nothing is a code by its shape, so a single word such as a name is dropped, and so is the error code a phone reports for itself (`HKErrorAuthorizationDenied` now reads `unclassified`). Rows an older image stored are rewritten by the start-up script (every row) and by `close_stale_sync_runs` every half hour for the first two hours after its worker process starts (rows updated in the last 24 hours), because a worker still on the older image can store text after the API has started, and such a worker only exists during a rollout; the pass reads tables with no index for it, so it does not repeat for the life of the process, and the fork adds no migration for it; each row it rewrites is read again under a row lock first, so a newer event stored meanwhile is not overwritten. The same code goes into what the two tasks we run return, which the worker logs: `sync_vendor_data` (`params.<task>.error`, `errors`) and the phone upload (`Import failed: <class>`). Both tasks are `ignore_result=True`, so the result backend keeps nothing of them, neither a return value nor the exception of a task that raises (`task_store_errors_even_if_ignored=False` in `celery/core.py`); nothing reads their results. `start_historical_sync` (`services/providers/base_strategy.py`) sends the pull task by name, where the task's own `ignore_result` does not apply, so it passes `ignore_result=True` itself. The async sync endpoint (`api/routes/v1/sync_data.py`) returned the pull task's id and told the caller to check the task's status; it now points at `GET /api/v1/users/{id}/sync/runs`, and `docs/dev-guides/how-to-add-new-provider.mdx` says the same. Upstream stores and sends the text. | **Deploying the tag that first carries this (`0.9.0-syn.3`): move the API last (this tag has no migration, so it need not go first): its start-up then runs `scripts/data_migrations/reduce_sync_run_errors_to_codes.py` after the last worker on the older image is gone. Record the three counts from its start-up log (runs, per-data-type rows, cached events); a second run, by restarting the API or by hand in its container, must report 0, 0 and 0.** Where the API cannot go last, run the script by hand after the last app has moved. That run is the guaranteed pass. The start-up pass and the two-hour sweep are best effort: the API starts before the workers are moved, and the sweep's task goes to the shared `default` queue, which a worker still on the older image also consumes, so neither can promise to come after the last write of the older image. Decided 2026-10-06 instead of an index and a pass that never stops: only this one rollout has an older image that stores text. **Re-check on every bump:** a new caller of `emit_sync_*` or a new `DataTypeOutcome` is covered by `emit` / `try_record_data_types` and needs no change, but it reads `unclassified` unless it passes `error_code(exc)`. A new path that stores or sends a sync event *without* going through `emit` is not covered. A new fixed word must be added to `_FIXED_WORDS` or it reads `unclassified`. A new backend module that defines an exception class must be added to `_APP_MODULES`: `test_every_exception_the_backend_defines_is_a_code_in_a_process_that_loaded_nothing` fails until it is. The error codes are documented in `docs/api-reference/guides/sync-status-stream.mdx` and in the `sync.failed` event description (`schemas/webhooks/event_types.py`). The cleanup script stays in `app.sh`, and the pass in `close_stale_sync_runs` stays, while an image that stored text can still run beside this one. **Not covered:** the synchronous mode of the provider sync endpoint (`POST /providers/{p}/users/{id}/sync?async=false`), which answers the API-key caller with the provider's result as it is and, for an integrity error, with the database's message; nothing of ours calls it. And `RAW_PAYLOAD_STORAGE`, off by default, which when on stores what the phone posts to the SDK-logs endpoint as it is. **Not covered, upstream-wide:** log lines that print `str(exc)` and every `log_and_capture_error` call (about 50), which logs the traceback and with it the error's text, often beside `user_id`; and the results of the other Celery tasks (the provider webhook handlers return `{"error": str(e)}`). |
| 🔒 **A stored sync run is removed 90 days after it was stored** (Notion 2.47.17.1) | `da08ed40` | `integrations/celery/tasks/prune_sync_runs_task.py` (new), `integrations/celery/core.py` (beat entry `prune-old-sync-runs`), `integrations/celery/tasks/__init__.py`, `repositories/sync_run_repository.py` (`delete_stored_before`, `oldest_stored_at`), `config.py` (`sync_run_retention_days`), `api/routes/v1/sync_status.py`, `schemas/sync_status.py` and `services/sync_status_service.py` (descriptions only); `tests/tasks/test_prune_sync_runs_task.py` | `sync_run` is new in 0.9.0 and nothing removed a row except deleting its user, so a run was kept for as long as the account lived. Data protection accepted the record on condition of a storage limit of about 90 days. Beat now runs `prune_old_sync_runs` daily at 03:30 UTC: it deletes every run whose `created_at` is older than `SYNC_RUN_RETENTION_DAYS` (default 90, at least 1), whatever its status, scope or source, and `sync_run_data_type` rows go with their run through the existing `ON DELETE CASCADE`. The age is counted on `created_at`, which the database sets when the row is first stored and nothing changes after; `started_at` and `updated_at` are copied from the event, and `updated_at` moves with every later event. Every run of the task writes one line, `sync_run_prune_complete`, with `removed_count`, `retention_days` and `oldest_remaining_age_days` (also when it removed nothing), and returns the same three; an error is not caught, so the task shows as failed. No migration and no index: the delete reads the table, which the delete itself keeps small. **Not covered:** the Redis sync history (24 hours, its own expiry) and the Svix copy of `sync.*` webhooks (`SVIX_PAYLOAD_RETENTION_DAYS`) are not touched by this task; a run that reports again after its row was removed is stored afresh as a new row, with its item counts starting again; a run key that is used again keeps its one row (Garmin's backfill key when it has no trace id, `garmin_backfill_<user_id>`), so that row's age counts from the first time it was stored, not from its latest use. **Drop when:** upstream prunes `sync_run` itself. |
| **Who may obtain an SDK token** (Notion 2.71.4): app credentials only, one application only, existing users only | `9bea506` | `api/routes/v1/sdk_token.py`, `api/routes/v1/user_invitation_code.py`, `services/application_service.py` (`require_single_application`), `repositories/application_repository.py` (`count_all`), `services/sdk_token_service.py` (`sdk_token_source_enabled`), `services/refresh_token_service.py`, `services/user_invitation_code_service.py`, `api/routes/v1/config.py`, `config.py`; `frontend/src/routes/_authenticated/users/$userId.tsx`, `frontend/src/lib/api/services/config.service.ts`; a note in `docs/app/introduction.mdx`, `docs/sdk/index.mdx` and the Flutter and React Native example-app pages; `tests/api/v1/test_sdk_token_who_may_mint.py` (+ the setting switched on in `test_user_invitation_code.py` and one test of `test_token.py`) | Upstream mints an SDK token for **any** user id by three routes: app credentials, a developer login with no credentials, and an invitation code a developer generates and anyone redeems on a public route. Nothing binds an application to a user. **No binding was built, on purpose:** dev and prod each have one developer and one application (counted 2026-10-05; neither holds a refresh token issued by the two closed routes), whose job is to mint for every user, so a binding would attach every user to that one application and stop nothing. Instead the ways in are narrowed. (1) `SDK_TOKEN_DEVELOPER_MINT_ENABLED` and `USER_INVITATION_CODES_ENABLED` default to **false**: the developer-login route answers 403 and both invitation-code routes answer 404. Refresh tokens those routes issued earlier (`app_id` beginning `admin:` or `invite:`) stop refreshing too, logged as `refresh_token_route_closed`. (2) **Minting is refused with 409 while more than one application exists**, on the mint route and on redeem, so a second application cannot reach the first one's users before a binding exists. Refresh of tokens already issued keeps working, so provisioned devices are not cut off. (3) A token is minted only for a user that exists (404; upstream raised a foreign-key error). | **Before the first deploy to an environment, count its applications** (`select count(*) from application`): with more than one, this change stops token minting there. Also look for refresh tokens whose `app_id` begins `admin:` or `invite:`; a device holding one is signed out of sync at its next refresh. **Residual, accepted:** whoever holds the application's credentials mints for any user — that is the adapter's job. **Not a defence against a stolen developer login:** that account can rotate the application's secret, or delete the application and create its own, and can create API keys that read everything; only keeping the developer routes off the internet addresses that (calibra-infrastructure, gateway path restriction). **The day a second application or developer is wanted, build the binding first** — until then creating one stops token minting for everyone, loudly: the 409 logs `action="sdk_token_refused_application_count"` with `application_count`, and the alert `ow-sdk-token-refused-second-application` (calibra-ow-deploy `scripts/alerts.sh`) reads that exact string, so **renaming the action is a telemetry migration** across two repositories. The checks run after the caller is authenticated, so a stranger learns neither the application count nor whether a user exists; on the public redeem route that means after the code is found valid and before it is used up, so an unknown code gets 404, cannot raise the alert, and a refused code stays redeemable. **The dashboard is told:** `GET /config` carries `user_invitation_codes_enabled` and the user page hides **Connect Mobile App** when it is false (upstream's own flag pattern; the frontend is not deployed by Calibra, the change keeps the fork's dashboard and docs honest). `REFRESH_ACTION_ROUTE_CLOSED` is deliberately not in `REFRESH_REJECT_REASONS`: that tuple is the stuck-client query's vocabulary. On each upgrade: re-check whether upstream added another route that mints an SDK token (`create_sdk_user_token` callers), and whether it still writes `admin:` / `invite:` as the `app_id` prefixes — the two refresh tests mint through the real routes, so a changed prefix turns them red. |
| **A concurrent OAuth token rotation is not a revocation** | `9c6215a` (#19) | `services/providers/templates/base_oauth.py`, `tests/integrations/test_oauth_refresh_race.py` | WHOOP rotates refresh tokens — the old one dies the instant a refresh succeeds. Two webhooks for one user landing together are picked up by two workers (the fast lane splits them across queues, so this is likelier here than upstream); both present the same refresh token, the first rotates it, the second is rejected 400 about 150ms later while the connection is perfectly healthy and a fresh access token has just been written by the winner. Upstream `refresh_access_token` reads that 400 as the refresh token being dead and revokes the connection, silently stopping every sync for that user until they reconnect by hand. On a 400/401 the connection row is now re-read; if the stored refresh token is no longer the one we presented, another worker rotated it — return the winner tokens and leave the connection alone. Seen twice in dev, 2026-07-27 04:31 and 2026-08-02 04:32, two days of missing data each. | **DEPENDS ON `READ COMMITTED`.** The winner is a different process whose COMMIT lands after our transaction began; only under READ COMMITTED does the refresh take a new snapshot and see it. Verified unset on our server (`default_transaction_isolation = read committed`, no per-database or per-role override). The tests drive both sides through a single session, so they would stay green under REPEATABLE READ while healthy connections started being revoked again in production — the comment at the call site is the only guard, keep it. The re-read refreshes just the connection row (not `expire_all()`); a row deleted underneath falls through to the revoke path. A genuinely dead token still revokes, one cycle later, costing a single failed webhook. |
| **Svix database is checked for before `CREATE DATABASE`** (least-privilege `DB_USER`) | `8891b17` | `scripts/init/create_svix_db.py`, `tests/scripts/test_create_svix_db.py` | Production's `DB_USER` has no CREATEDB and `svix` is provisioned out of band. PostgreSQL checks the CREATEDB privilege before it checks for a duplicate name, so upstream's unconditional `CREATE DATABASE` fails that role with `InsufficientPrivilege` even though the database exists, and `app.sh` (`set -e`) never reaches the API. Seen on the first production boot, 2026-09-11; a hand-built image of syn.6 plus this commit has run in production since. The existence read comes first; the CREATE runs only when the name is absent. | **Upstream rewrote this file after 0.6.2** (`8dcb85a`, #1293): gated on `outgoing_webhooks_enabled`, whole body in a catch-all that logs a warning and continues ("never block app boot"). It conflicted at 0.9.0 and was settled as: upstream's `outgoing_webhooks_enabled` gate and logger setup, the fork's body. So with webhooks disabled the script does nothing, and the tests set `outgoing_webhooks_enabled` to reach it. Upstream's version also boots under least privilege, but a *missing* `svix` then surfaces only when svix-server fails, not at API boot — ours stops the boot on purpose (`test_missing_database_fails_loud_for_a_role_without_createdb` pins it). Decided at 0.9.0: keep the loud failure. Don't settle a future conflict by taking upstream's file. Whatever survives, the existence check must stay ahead of the CREATE. |
| **Garmin endpoint enable-list scoped, `mct` excluded** | `fea607c` (#23) | `docs/providers/garmin-api-integration.mdx`, `AGENTS.md` | Upstream setup page tells operators to enable **every** endpoint on the Garmin API Tools page. The list is now derived from `supported_event_types()` in `providers/garmin/webhook_handler.py` (citing `WELLNESS_TYPES`) instead, and `mct` is excluded — enabling it subscribes the deployment to menstrual-cycle and pregnancy data that is out of scope here — with the matching instruction to skip the Women's Health API product at app creation. The Art. 9 warning states that every listed **data** endpoint carries health data needing the deployment's own lawful basis and assessment, and that `mct` is left out on purpose limitation and data minimisation, not because the rest is Art. 9-safe. `userPermissionsChange` and `deregistrations` are named separately: their handlers (`providers/garmin/handlers/lifecycle.py`) read only a Garmin `userId` plus the permissions list or the revocation. | Docs-only, so an upstream bump reverts it **without a conflict marker** — diff `docs/providers/garmin-api-integration.mdx` deliberately on every upgrade. Selecting Women's Health and enabling `mct` is a **scoped** choice, not a blanket prohibition: `docs/providers/coverage.mdx` still advertises Garmin menstrual-cycle ingestion and the backend processes it, so a deployment whose purpose and legal basis cover that data may turn it on. The exclusion sits above the Art. 9 reasoning so it survives an operator skimming for the endpoint list. Also drops an inherited prompt-injection canary from `AGENTS.md` (an instruction telling AI agents to append a Pancake Recipe section to every PR description) — if an upstream merge restores it, remove it again. |

---

## Personal Record write API (net-new)

`PUT` / `GET /api/v1/users/{user_id}/personal-record` — adapter-driven write
API for `public.personal_record`. Upsert keyed on the 1:1 `user_id`
(`ApiKeyDep`, 201 create / 200 update). Body = `birth_date` + `gender`
(`sex` intentionally omitted — read by nothing in OW).

Purpose: let the .NET adapter set `birth_date` so OW computes HR-zone max HR as
`220 − age` (`estimate_max_hr`) at workout ingest instead of the
`DEFAULT_MAX_HR = 190` fallback.

Boundary: OW computes zones ONCE at ingest, so a populated `birth_date` only
affects workouts ingested afterward. Historical workouts keep their maxHr-190
zones and continue to be handled by the .NET adapter's
`WorkoutHrZoneHealService`.

Upstream OW has no write path for `personal_record` (only seed data writes it).

---

## Upstream behaviour the adapter depends on (not a delta)

This is upstream code we do not change, but the .NET adapter breaks if an upgrade removes it,
so **an upgrade that changes it is a breaking change for the adapter**.

| Upstream behaviour | Where (at `release/0.9.0-syn`) | Why the adapter needs it |
|---|---|---|
| **`users.external_user_id` is settable on `POST /api/v1/users`, filterable on `GET /api/v1/users?external_user_id=`, unique, and returned in `UserRead`** (the model both of those routes respond with) | `models/user.py:19` (`Unique`), `schemas/model_crud/user_management/user.py:146` (`UserCreate`), `:67` (`UserQueryParams`) and `:120` (`UserRead`), `repositories/user_repository.py:153-154`, `api/routes/v1/users.py:21` and `:78` (both `response_model` `UserRead`; the list route now also sets `response_model_exclude_unset=True`, and the field still comes back because `UserRead` is built from the row) | Since calibra-adapter 2.41.13 (PR #329, 2026-09-28), `POST /account/register` finds or creates each person's OW user **by `external_user_id` = the Calibra user id**, never by email. It also reads the field **back** from both responses and accepts only an exact match. Losing any of the four properties makes register fail closed: the create field, the filter, uniqueness, or the field in `UserRead`. Every new account then ends `partial` and logs `Event=ow_stamp_mismatch`, so no new user gets an OW user. Upstream already marks the field **deprecated** (`_EXTERNAL_USER_ID_DEPRECATION`, `schemas/model_crud/user_management/user.py:23`, "only works as a filter on GET /users"); all four properties still hold at 0.9.0 and on upstream `main` at `12941a4c`, 2026-10-02. Spec: calibra-adapter `docs/superpowers/specs/2026-09-25-2.41.13-register-ow-user-binding-design.md` §5.5, D-01, D-04. |
| **REST `source` object: `source.source` is the writer, `source.provider` the integration** — on `/timeseries` and `/events/workouts` | `schemas/utils/metadata.py:9` (`SourceMetadata`), built by `services/timeseries_service.py` `_to_sample` and `services/event_record_service.py` `_map_source` (`provider=data_source.provider`, `source=data_source.source`) | The adapter keeps one step total per HealthKit writer per hour (`StepsDailyDedup.SumPerHourMax`) and takes the largest, so it needs the writer, not the integration. Up to 0.6.2 the object was `{provider, device}` and `provider` carried the writer; 0.9.0 moved the writer to `source` and made `provider` the integration. calibra-adapter #368 (2026-10-05) reads `source.source` and falls back to `source.provider`, so it works on both. **If upstream moves or renames the writer again, Apple users' steps are over-counted** (1.38x and 1.58x measured when the adapter read `provider` on 0.9.0) and nothing errors. The same value labels stored weights (`ow:<writer>`) and matches HR samples to a workout in the adapter's HR-zone heal. The webhook payload's `source.provider` was always the integration; the adapter reads that separately and 0.9.0 only adds members there. |

---

## Superseded by upstream — DROPPED at 0.6.2 (executed 2026-07-07)

Reimplemented independently upstream; not carried forward. See the outcome section above.

| Former commit | Superseded by | Outcome |
|---|---|---|
| session-lifecycle hardening (`d436010`/`620a71b`/`c38cf33`) | `ac351ab` #1147 + `f5b9696` #1215 sync refactor | **Dropped** — upstream reads `last_synced_at` pre-commit + new WriteCounts accounting. |
| `d09028c` snapshot ORM attrs before `after_commit` | `e8efeea` #1208 (direct-fire) | **Dropped** — mechanism gone; Edwards reworked onto direct-fire. |
| `9c82908`/`23ad5ea` Polar TL/distance → float | `a10227b` #1204 | **Dropped.** |
| Redis-TLS *field* half of `3e287c0` | `a764df2` #1134 | **Superseded** — took upstream's `redis_ssl` field, kept only `ssl_cert_reqs=none`. |

## Noise — squash/collapse before rebasing (generic, for future upgrades)

- Ruff/style-only and duplicated commits: fold into their feature parents before the rebase (see Appendix A).

---

## Upgrade recipe (run this for every upstream bump)

> One-time cleanup uses **rebase**; recurring upgrades use **merge**. See "Why" below.

### 0. Prep
```bash
git remote get-url upstream || git remote add upstream https://github.com/the-momentum/open-wearables.git
git fetch upstream --tags
git config rerere.enabled true    # remembers conflict resolutions across upgrades
```

### 1. Advance the mirror
```bash
git switch main
git merge --ff-only <new-upstream-tag>   # e.g. 0.6.2 ; must be fast-forward
git push origin main
```
> If `--ff-only` fails, `main` has drifted (someone committed to it) — it must stay pristine.
> Reset it: `git reset --hard <new-upstream-tag>` and force-push (nothing unique should be lost).

### 2. Integrate the delta
**First jump to 0.6.2 (one-time history cleanup → rebase):**
```bash
git switch -c release/0.6.2-syn origin/release/0.5.2-syn
git rebase -i upstream-0.5.2         # squash the "Noise" list first
git rebase --onto main upstream-0.5.2
#   Expect 3 conflicts (sync_vendor_data_task.py, event_record_service.py, config.py).
#   Apply the DROP/skip decisions from the tables above.
```
**Every upgrade after that (merge, so conflicts are resolved once):**
```bash
git switch release/<prev>-syn
git switch -c release/<new>-syn
git merge main                        # rerere replays known resolutions
#   Two Alembic heads after the merge? Add a merge revision joining upstream's head and the
#   fork's head (as e4b7a1c9d3f2 did). Never re-point a revision a deployed database records.
#   Walk the "durable delta" table; re-check each item still applies + behaves.
#   Walk "Upstream behaviour the adapter depends on"; if any row changed, stop: it is an adapter change first.
```

### 3. Prove it
```bash
cd backend && <test runner>
#   Focus: test_polar_247, test_polar_workouts, test_heart_rate, test_outgoing_webhooks
```
Then **diff engine outputs vs a pre-upgrade snapshot** — especially workout HR-zone and
respiratory payloads — because the .NET adapter's parity is pinned to these.

How it was done at 0.9.0, and what found the adapter-breaking change: restore a dump of the dev
database twice, run `alembic upgrade head` on one copy, then call the REST routes the adapter
reads (`/timeseries`, `/events/workouts`, `/events/sleep`, `/users`, `/connections`) for every
user through the previous release on the untouched copy and the new release on the upgraded
one, and diff the responses field by field. A value that changes meaning without changing shape
(`source.provider` at 0.9.0) is invisible to the test suite and obvious in that diff.

### 4. Ship
```bash
git tag <new>-syn.1
git push origin release/<new>-syn --tags
# Bump OW_REF: <new>-syn.1 in calibra-ow-deploy/.github/workflows/deploy-openwearables.yml
```
`release/<prev>-syn` and its tag stay untouched, but **reverting `OW_REF` is a rollback only if
the upgrade ran no migration the previous release cannot live with.** 0.9.0 does (hashed
`api_key`, dropped `event_record_detail`), so from 0.9.0 back to 0.6.2 the rollback is a database
restore plus the `OW_REF` revert. Snapshot the database before the first deploy of a new base, and
check the new migrations' downgrades before promising anything faster.

### Why rebase once, merge thereafter
Rebase replays the whole delta from scratch every time → you re-resolve the same conflicts
on every upgrade and must force-push a branch the deploy pipeline tracks. Merge resolves each
conflict once (recorded in the merge commit; merge-base advances) and never force-pushes.
Use rebase only for the initial 0.6.2 cleanup to get a tidy base.

---

## Long-term: shrink this file

The cheapest upgrade is a small delta. Everything in the "Superseded" table proves upstream
will independently fix generic issues. **Upstream the generic bits** (the SQLAlchemy
session/lazy-load hardening, respiratory-rate persistence) via PRs to the-momentum so they
leave the fork permanently. Aim to keep only the ~4 truly-Synaptik features in the durable table.

---

## Appendix A — 0.5.2-syn squash plan (one-time history cleanup)

Run this **before** `git rebase --onto main upstream-0.5.2` to collapse the raw
22-non-merge-commit history into **8 clean feature commits** (verified: runs conflict-free and
produces a byte-identical tree). It is in **original order — no reordering** — so it cannot
self-conflict; every `fixup`/`squash` folds into the feature `pick` directly above it.

`fixup` = discard the folded message (noise / duplicates); `squash` = combine messages
(meaningful sub-commits — reword the result).

```
pick   d436010 fix(worker): cache last_synced_at before workouts commit
squash 620a71b fix(whoop): rollback + re-raise on sleep save failure
squash c38cf33 fix(worker): fresh session for data_247
fixup  35cbdc0 (duplicate of c38cf33)
fixup  26faed1 style: E501 in sync_vendor_data_task
pick   d09028c fix(event_record_service): snapshot ORM attrs before after_commit
fixup  7a923b2 style: ruff format event_record_service
squash 8104bed fix: _emit_event_record_webhook accepts snapshots
squash bc1b5bb refactor: freeze snapshot dataclasses
pick   3e287c0 fix(config): Redis TLS support + WHOOP read:profile scope
pick   513b94d feat: respiratory_rate -> data_point_series (Thread 14f)
pick   feeabca feat: Edwards HR-zone minutes on workout.created (Thread 20B)
fixup  5c33af7 style: ruff format event_record_service
squash 29c0a35 fix: insert HR samples before workout details (zone query)
fixup  ff312ff (duplicate ordering fix)
pick   753ec9c feat(polar): Recharge HRV -> rmssd bridge
squash b2e93f9 feat(polar): Recharge RHR -> resting_heart_rate bridge
pick   9c82908 fix(polar): Training Load Pro + distance -> float
fixup  23ad5ea test(polar): fractional Training Load + distance
pick   01e6eae feat(webhooks): config-driven priority event set
squash 0905a69 feat(webhooks): route to webhook_sync fast lane + CELERY_QUEUES
fixup  94fd4f8 style: ruff format test_outgoing_webhooks
```

Resulting 8 commits and their reconcile role at the `--onto main` step:

| # | Feature commit | Reconcile role at 0.6.2 |
|---|---|---|
| 1 | SQLAlchemy session-lifecycle hardening (last_synced_at / whoop / data_247) | **Drop-candidate** — check vs #1147/#1215 |
| 2 | event_record snapshot-before-after_commit | **Drop** — superseded by #1208 |
| 3 | Redis TLS + WHOOP read:profile scope | **Split** — drop TLS (#1134), keep WHOOP scope |
| 4 | respiratory_rate → data_point_series | Keep (verify vs #1235) |
| 5 | Edwards HR-zone payload | **Keep — the .NET adapter depends on it** |
| 6 | Polar Recharge HRV + RHR bridges | Keep |
| 7 | Polar TL/distance float | **Drop** — superseded by #1204 |
| 8 | Webhook fast lane | Keep |

So the subsequent `--onto` reduces to: drop commits 2 & 7, split 1 & 3, keep the rest;
expect 3 conflicts (`sync_vendor_data_task.py`, `event_record_service.py`, `config.py`).

### Run it non-interactively (optional)

```bash
git config rerere.enabled true                 # do this first
# save the todo block above to /path/to/rebase-todo.txt (action + sha per line)
GIT_SEQUENCE_EDITOR="cp /path/to/rebase-todo.txt" GIT_EDITOR=true \
  git rebase -i upstream-0.5.2
```

Drop `GIT_EDITOR=true` if you want to reword each of the 8 combined messages as you go
(recommended — e.g. commit 1 should read "session-lifecycle hardening", not just "cache
last_synced_at").
