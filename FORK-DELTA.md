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
  from `OW_REPO = Synpatik-GmbH/open-wearables` at the tag in `OW_REF` (currently `0.6.2-syn.8`).
- Deploys always reference an **immutable tag**, never a branch head.

## Current base

- **Now on: 0.6.2** (`a07818f`) — upgraded from 0.5.2 (`a2060c7`) on 2026-07-07.
- `release/0.6.2-syn` = the 0.6.2 mirror plus the delta below; latest tag `0.6.2-syn.8`, deployed to dev 2026-09-15.

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

Commit hashes below are the **current `release/0.6.2-syn`** shas (update each upgrade).
Multi-commit deltas cite the **merge** commit and its PR number.

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
| **Edwards HR-zone payload** on `workout.created` | `15056f2` | `event_record_service.py`, `heart_rate.py`, `data_point_series_repository.py`, `outgoing_webhooks/events.py` | **The .NET adapter depends on this** — its `WorkoutHrZoneHealService` replicates the EXACT Edwards algorithm. Dropping/changing it breaks parity. | ⚠️ HIGHEST-RISK. Delivery is **deferred**, not direct-fire: since `67227bb` (#18) `create_detail` / `bulk_create_details` schedule a `finalize_workout_zones` settle-debounce task after commit and there is **no synchronous emit at ingest** for workouts (`event_record_service.py:761`); only sleep / menstrual still emit synchronously. Zones are computed by that task from the settled trace, not while the ingest session is live. Re-verify payload shape against the adapter after any upstream zone change. |
| **Edwards HR-zone minutes on REST `/events/workouts`** | `862a29c` | `event_record_service.py` (`_compute_workout_hr_zone_fields`, `get_workouts`), `schemas/responses/activity/events.py` | REST counterpart of the webhook delta above. Upstream `Workout` carries no zone fields, so the adapter's **reconcile** pull got no zones → `background_hr` → downgraded an already-`edwards` row. Computes zones on read via the same `get_workout_hr_zone_minutes`. | Same `hr_zone_N_min` + `hr_trace_completeness` field names as the webhook payload (single source of truth). Compute-on-read (no persistence), so historical workouts self-heal. |
| **`workout.created` deferred to a settle debounce** | `67227bb` (#18) | `services/event_record_service.py`, `integrations/celery/tasks/finalize_workout_zones_task.py` (new), `integrations/celery/tasks/__init__.py`, `services/apple/healthkit/workout_settle.py` (new), `config.py` (`workout_zone_debounce_seconds`, `workout_zone_target_completeness`, `workout_zone_hard_cap_seconds`), `tests/conftest.py`, `tests/integrations/celery/test_finalize_workout_zones.py`, `tests/services/test_workout_settle.py`, `tests/services/test_event_record_service.py`, `tests/integrations/test_apple_sdk_import.py`; `docs/specs/` + `docs/plans/2026-07-12-workout-created-debounce-hr-settle.md` | A workout's HR trace does not all arrive in the upload that creates the workout, so zones computed at ingest are partial and upstream's single direct-fire `workout.created` shipped whatever happened to be there. `create_detail` / `bulk_create_details` now schedule one `finalize_workout_zones` task per workout after commit; it re-polls trace completeness and emits **once** — at `workout_zone_target_completeness` (0.95) or when `workout_zone_hard_cap_seconds` (5s) elapses. Sleep / menstrual keep the synchronous after-commit emit. | ⚠️ **This is the delivery mechanism of the HIGHEST-RISK Edwards row above**, so any upstream change to after-commit webhook dispatch conflicts here first. `tests/conftest.py` autouse-mocks `finalize_workout_zones.apply_async` — an upstream `conftest.py` conflict that loses that fixture makes unrelated suites enqueue real Celery work. Needs Redis (settle marker) and the `default` queue; the fast lane is not involved. Emit-once is the contract: two emits is a worse regression than a late one. |
| **HealthKit inserts the HR trace before workout details** | `a438b10` (#16, `6f4e4ed`) | `services/apple/healthkit/import_service.py`, `tests/integrations/test_apple_sdk_import.py` (+ a `ty` silencing in `services/personal_record_service.py`, `ce5da51`) | `load_data` created workout details — which compute the Edwards zones for `workout.created` from the `heart_rate` series **present in the DB** — before inserting the same upload's HR trace (the records section). A workout whose per-minute HR shipped in the SAME HealthKit upload still emitted null `hr_zone_*_min`, and the adapter showed the lower background-HR load until an hourly heal re-derived Edwards. All HR samples (workout-embedded + records trace) are now inserted and flushed before `bulk_create_details`. | Re-applies an ordering guarantee **already lost once to an upgrade** (0.5.2-syn `29c0a35`, dropped by the 0.6.1 direct-fire rework) — check it explicitly every time. Regression test: a workout co-uploaded with its per-minute HR trace must emit non-null `hr_zone_3_min`. The `#18` debounce reduces but does not remove the need for the ordering — the settle task recomputes zones, but the completeness signal it polls is this same trace. |
| **Webhook fast lane** | `9519029` | `outgoing_webhooks/events.py`, `scripts/start/worker.sh` | Latency: priority events → dedicated `webhook_sync` Celery queue (`CELERY_QUEUES`). Consumed by `aca-ow-worker-priority-dev`. | Keep the `worker.sh` CELERY_QUEUES override. |
| **Polar Recharge bridges** | `738186b` | `services/providers/polar/data_247.py`, `polar/coverage.py` | HRV→`rmssd`, RHR→`resting_heart_rate`. Not in upstream. | Must stay declared in `polar/coverage.py` (test_provider_coverage). |
| **respiratory_rate → data_point_series** (Thread 14f) | `1d2f46a` | `data_point_series_repository.py`, `whoop/coverage.py`, sleep save path | Sleep-side RR persistence. | Declared in `whoop/coverage.py`. Upstream `eddf5d9` #1235 is Garmin-side RR (complementary). |
| **WHOOP `read:profile` scope + Redis `ssl_cert_reqs=none`** | `565ae6a` | `config.py`, `tests/utils_tests/test_redis_url.py` | WHOOP app needs the scope; Azure Redis Enterprise proven with `none`. | Uses upstream's `redis_ssl` field → deploy env is **`REDIS_SSL`**. Two `test_redis_url` assertions carry the `none` delta. |
| **`GET /oauth/{provider}/authorize` requires a credential** (2.41.8) | `23d2ce2` | `api/routes/v1/oauth.py`, `tests/api/v1/test_oauth.py`; docs half at `10e66aa` — `docs/api-reference/guides/error-handling.mdx`, `docs/dev-guides/how-to-add-new-provider.mdx` | Upstream leaves it unauthenticated: any caller could mint OAuth state for any `user_id` and bind their own wearable to that user, or plant a `redirect_uri` the callback follows. `ApiKeyDep` (API key or developer JWT) closes it. The .NET adapter already sends `X-Open-Wearables-API-Key` on this call. | Upstream's own docs (`quick-integration.mdx`, `integration-guide.mdx`) already send the API key on this call. On each upgrade, check whether upstream's `authorize_provider` now carries an auth dependency; if it does, drop this delta. **Breaks the frontend `/users/$userId/pair` page**, which calls authorize with no credential; deliberate, the frontend is not deployed. |
| **SDK auth rejects a token whose user no longer exists** (2.41.9.1) | `004b009` | `utils/auth.py` (`get_sdk_auth`, `_parse_sdk_subject`), `tests/utils_tests/test_sdk_auth.py`, `tests/api/v1/test_sdk_sync_auth.py`, `tests/api/v1/test_sdk_logs.py`, `tests/services/test_sleep_service.py`; docs note in `docs/sdk/{ios,android,flutter,react-native}/integration.mdx`, `docs/sdk/index.mdx`, `docs/dev-guides/integration-guide.mdx` | Upstream decodes the SDK JWT and trusts `sub`, so a deleted user's token keeps getting 202 for the 60-minute access-token lifetime. A decodable SDK token is now a credential only while its user row exists; an API key alongside cannot rescue it; a lookup failure propagates. | On each upgrade, check whether upstream's `get_sdk_auth` gains a subject lookup; if it does, drop this delta. Tests that mint SDK tokens must create the user first. |
| 🔒 **Svix never receives a readable identifier** — hashed `eventId`, pseudonymous user channels | `4a7db35` (#22) | `services/outgoing_webhooks/pseudonyms.py` (new), `services/outgoing_webhooks/svix.py`, `services/outgoing_webhooks/events.py`, `api/routes/v1/outgoing_webhooks.py`, `config.py` (`svix_pseudonym_secret` + `derive_svix_pseudonym_secret`), `config/.env.example`, `scripts/data_migrations/pseudonymise_svix_user_channels.py` (new), `tests/services/test_svix_pseudonyms.py`, `tests/scripts/test_pseudonymise_svix_user_channels.py`, `tests/api/v1/test_outgoing_webhooks.py`; `docs/api-reference/guides/webhooks.mdx`, `docs/data-migrations/pseudonymise-svix-user-channels.mdx`, `docs/data-migrations/index.mdx`, `docs/docs.json` | Upstream sends a readable `eventId` (user id + provider + metric + ingestion window) and a `user.<uuid>` channel on every message. svix-server v1.69.0 keeps `message.uid` and `message.channels` on the message row **with no expiry** — payload retention reaches only `messagecontent`, there is no config key and no API operation, and the prune subcommand that would remove them only exists from a much later version. So every emitted event wrote a permanent copy of the user id into Svix. `eventId` is now an HMAC-SHA256 hex digest (input string unchanged, so dedup is unchanged); user channels are `user.` + AES-SIV of the user id, hex (deterministic, so endpoint filtering still matches; reversible with the key, so the endpoints API still reports `user_id`). **Survival: this is the control that makes Svix erasable at all.** Lose it and every webhook resumes writing a readable user id into a store with no deletion path — an Art. 17 erasure request cannot be satisfied for anything already delivered, and the DPIA statement that the webhook bus carries no readable identifier is false from the first event after the bump. That is a change of processing, not a lost feature. | `send()` pseudonymises at the **Svix boundary**, not only at the nine producers, so a Celery job enqueued by the previous release during a rolling deploy cannot smuggle a readable channel through. `migrate_legacy_user_channels()` is a **one-shot operator script**, not startup work — run `scripts/data_migrations/pseudonymise_svix_user_channels.py` once after upgrading; idempotent, exit 1 when Svix is unconfigured or a request failed (just rerun). Existing Svix rows are deliberately not rewritten. `SVIX_PSEUDONYM_SECRET` when unset is **derived** from `secret_key` under a fixed context, never equal to it (digests are returned to API callers and must not be HMAC outputs of the key that signs access tokens); rotating it changes every pseudonym — events already in Svix stop deduplicating against new ones, and a user-filtered endpoint stops receiving until its `user_id` is saved again (and then only after Svix endpoint cache refresh). `test_no_module_builds_a_readable_user_channel` sweeps `app/` for any `user.` literal or `USER_CHANNEL_PREFIX` use outside `pseudonyms.py` — it is what catches an upstream merge reintroducing a raw channel. |
| 🔒 **5-day Svix payload retention + endpoint-less-developer skip** | `e70564f` (#21) | `config.py` (`svix_payload_retention_days`, `Field(ge=5, le=90)`), `services/outgoing_webhooks/svix.py`, `integrations/celery/tasks/emit_webhook_event_task.py`, `config/.env.example`, `tests/test_config_svix_payload_retention.py`, `tests/api/v1/test_outgoing_webhooks.py`; `docs/api-reference/guides/webhooks.mdx`, `docs/api-reference/guides/sync-status-stream.mdx` | svix-server has no server-level retention setting, so retention is set **per message at creation**, and upstream sets none — which means the SDK default of **90 days**, confirmed against live data in both environments. Every payload (full sleep, workout and HR series for one identified user) sat in Svix for 90 days. 5 days is now set explicitly: the platform floor, still far clear of the 27h 35m retry tail (`[5, 300, 1800, 7200, 18000, 36000, 36000]`s) and well inside the one-month erasure response window. The emit task also looped **every** developer account, storing one copy of each payload per developer whether or not that account could receive it; it now emits only to developers with at least one registered endpoint, and no longer creates a Svix application for an account that never registered one. **Survival: the regression is completely silent.** Drop the per-message `payloadRetentionPeriod` in a merge and Svix accepts the message and quietly applies its own 90-day default — no error, no failing test, and the storage-limitation period the DPIA records (5 days) becomes 90. The per-developer fan-out is the same shape: extra copies of health data, nothing fails. Both are Art. 5(1)(e) commitments, not tuning. | `payloadRetentionHours` does **not** work: svix-server accepts it with a 202 and then ignores it (an earlier revision of this branch used 48h and messages still expired at 90 days). Only `payloadRetentionPeriod` is honoured, on both v1.69.0 and 1.101.0, and only for 5—90 days — hence `Field(ge=5, le=90)`, so an out-of-range deploy fails configuration validation at startup instead of 422-ing every webhook at runtime. Also in this PR and easy to lose separately: an **unreachable** Svix no longer counts as no-endpoint (`has_endpoints` returned `False` on `ConnectError`, so the task acked and the event was dropped with no send and no retry). Only an answer that *proves* there is no endpoint — an empty list, or a 404 for an application never created — may skip; every lookup that merely failed returns `True` and defers to `send()`, which owns the delivery-failure contract. Every swallowed lookup failure, `ConnectError` included, goes through `log_and_capture_error` → Sentry (`783635e`), as `backend/AGENTS.md` requires for handled exceptions in background paths. |
| 🔒 **SDK refresh-token rotation survives a lost reply** | `b8debf6` (#20) | `services/refresh_token_service.py`, `repositories/refresh_token_repository.py`, `models/refresh_token.py` (`rotated_from`), `api/routes/v1/sdk_token.py`, `config.py` (`sdk_refresh_grace_seconds` + `_validate_sdk_refresh_grace_seconds`), `migrations/versions/2026_09_11_1200-7c3e9a41d2b8_refresh_token_rotated_from.py`, `tests/services/test_refresh_token_{service,logging}.py`, `tests/repositories/test_refresh_token_repository.py`, `tests/integrations/test_sdk_refresh_concurrency.py`, `tests/integrations/sdk_refresh_race_support.py`, `tests/api/v1/test_token.py`, `tests/test_config_sdk_refresh_grace.py`; `docs/sdk/{ios,android,flutter,react-native}/integration.mdx`, `docs/superpowers/specs/2026-09-11-sdk-refresh-token-lost-rotation-design.md`, `docs/superpowers/plans/2026-09-11-sdk-refresh-token-lost-rotation.md`, `docs/superpowers/specs/queries/2026-09-11-sdk-refresh-stuck-clients.kql` | Upstream revokes the presented refresh token the instant rotation succeeds. If the reply never reaches the phone (dropped connection, backgrounded app), the successor it never saw is the only live token and **every later refresh is 401, permanently** — the only way out is reconnecting by hand, and nothing in the app says so. A superseded token whose successor exists and is still unrevoked now returns that same successor, within `sdk_refresh_grace_seconds` (default 7 days); grace writes no rows and is idempotent. Rotation is serialised per `(user, app)` with a `pg_advisory_xact_lock` and successors are linked through `rotated_from`. **Survival: a locked-out phone keeps recording and stops delivering, so nothing alerts.** The user is never told, no error surfaces, and the health record OW holds for that person is silently incomplete for the whole period — an accuracy and availability failure (Art. 5(1)(d), Art. 32) the DPIA counts as a control, not a UX nicety. Detection depends on it too: the stuck-client KQL query only works because every refresh outcome is logged through the D-08 constant, so a merge that drops the logging also blinds the alarm. | Adds a column, so the Alembic revision travels with the delta (`7c3e9a41d2b8`, down `9f0940493a9b`). Also here and separable by accident: a fresh SDK mint revokes the earlier live tokens for that `(user, app)` (Calibra is one phone per account) and those revocations write **no** `rotated_from`, so a mint-revoked token is final and never handed back through grace; revoking a superseded token also revokes its unused successor, so logging out after a missed rotation cannot leave a live token reachable. Developer tokens are untouched on every path (D-04) and the invitation-code mint path is out of scope. All five DB operations sit in `RefreshTokenRepository` (`backend/AGENTS.md:297`) and **none of them commit** — pushing a commit into any one would release the advisory lock early. On each upgrade, check whether upstream rotation grew a grace window of its own. |
| 🔒 **`user_id` out of the SDK-logs summary line** | `2f2112b` (#25) | `api/routes/v1/sdk_logs.py` | `submit_sdk_logs` discards the body — `store_raw_payload` returns immediately while raw-payload storage is disabled, and nothing is persisted or forwarded — so its structured log line is the **only durable trace** of the endpoint, and `user_id` was the only personal datum in it. `batch_id`, `provider`, `event_count`, `event_types` and `sdk_version` are kept: no personal data, and they are what makes the line useful. Correlate on `batch_id`. **Survival: the observability window is a separate retention regime with no erasure path.** A deletion request reaches the database, not 30 days of log storage; once a user id is written there it stays for the full window whatever happens to the user row. One line of upstream reformatting puts the field back, nothing fails, and the DPIA statement that SDK log ingestion holds no personal data quietly stops being true. | This removes one copy, **not both**. The user id is also in the URL path, and `main.py:33-36` routes `uvicorn.access` to the same ingestion, so every request still writes it into the same window via its request line. That copy is **accepted and disclosed**, not fixed: suppressing paths in the global access log would cost 5xx debugging across the whole API to protect one route. Keep the comment above the `log_structured` call — no test pins the absence, so the comment is the only thing standing between this delta and someone re-adding `user_id` as an obvious improvement. |
| **A concurrent OAuth token rotation is not a revocation** | `9c6215a` (#19) | `services/providers/templates/base_oauth.py`, `tests/integrations/test_oauth_refresh_race.py` | WHOOP rotates refresh tokens — the old one dies the instant a refresh succeeds. Two webhooks for one user landing together are picked up by two workers (the fast lane splits them across queues, so this is likelier here than upstream); both present the same refresh token, the first rotates it, the second is rejected 400 about 150ms later while the connection is perfectly healthy and a fresh access token has just been written by the winner. Upstream `refresh_access_token` reads that 400 as the refresh token being dead and revokes the connection, silently stopping every sync for that user until they reconnect by hand. On a 400/401 the connection row is now re-read; if the stored refresh token is no longer the one we presented, another worker rotated it — return the winner tokens and leave the connection alone. Seen twice in dev, 2026-07-27 04:31 and 2026-08-02 04:32, two days of missing data each. | **DEPENDS ON `READ COMMITTED`.** The winner is a different process whose COMMIT lands after our transaction began; only under READ COMMITTED does the refresh take a new snapshot and see it. Verified unset on our server (`default_transaction_isolation = read committed`, no per-database or per-role override). The tests drive both sides through a single session, so they would stay green under REPEATABLE READ while healthy connections started being revoked again in production — the comment at the call site is the only guard, keep it. The re-read refreshes just the connection row (not `expire_all()`); a row deleted underneath falls through to the revoke path. A genuinely dead token still revokes, one cycle later, costing a single failed webhook. |
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
#   Walk the "durable delta" table; re-check each item still applies + behaves.
```

### 3. Prove it
```bash
cd backend && <test runner>
#   Focus: test_polar_247, test_polar_workouts, test_heart_rate, test_outgoing_webhooks
```
Then **diff engine outputs vs a pre-upgrade snapshot** — especially workout HR-zone and
respiratory payloads — because the .NET adapter's parity is pinned to these.

### 4. Ship
```bash
git tag <new>-syn.1
git push origin release/<new>-syn --tags
# Bump OW_REF: <new>-syn.1 in calibra-ow-deploy/.github/workflows/deploy-openwearables.yml
```
`release/<prev>-syn` and its tag stay untouched → instant rollback by reverting `OW_REF`.

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
