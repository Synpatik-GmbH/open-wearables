# SDK refresh-token rotation must survive a reply the client never applied

> Status: draft
> Owner: Dragan
> Last semantic change: 2026-09-11 (design-review round 2, concurrency mechanism; before acceptance,
> so no inbound references to sweep and no sweep report)
> Repo: open-wearables (fork) · Branch: `release/0.6.2-syn` (= tag `0.6.2-syn.6`)
> Type: Fork DELTA — changes behaviour inherited unchanged from upstream 0.6.2

Claims about code in other repositories cite the repository and file but no line number: the iOS
SDK, the Calibra app, the adapter and the deploy repo. They were checked on 2026-09-11, and this
repository's lint cannot resolve them. Claims about production behaviour cite the dev database
and logs described in §1.

---

## 1. Context

OW issues SDK refresh tokens and rotates them on every use. `refresh_token`
(`backend/app/services/refresh_token_service.py:80`) looks the presented token up among
**unrevoked** rows (`backend/app/services/refresh_token_service.py:96`, predicate at
`backend/app/repositories/refresh_token_repository.py:26`), rejects anything else with 401
(`backend/app/services/refresh_token_service.py:97`), revokes the presented token
(`backend/app/services/refresh_token_service.py:105`) and creates a successor
(`backend/app/services/refresh_token_service.py:113`). The revoke and the create are two
separate commits (`backend/app/repositories/refresh_token_repository.py:42`,
`backend/app/repositories/refresh_token_repository.py:20`). Nothing links a token to its
successor (`backend/app/models/refresh_token.py:30` is the only revocation field). This code is
byte-identical to upstream `0.6.2`: `git diff 0.6.2 0.6.2-syn.6` on the service, repository, model
and route files is empty.

**The failure.** If the client does not apply the rotation reply, it keeps the revoked token. Every
later refresh presents that token and gets 401. The only way back is a fresh mint
(`POST /api/v1/users/{id}/token`, `backend/app/api/routes/v1/sdk_token.py:71`). In Calibra that
means the user disconnecting and reconnecting Apple Health by hand: calibra-app
`lib/onboarding/connectWearable.ts` is the only path that signs the SDK in, and it always uses a
freshly minted pair. Until then no HealthKit data reaches OW.

**Why the reply goes missing.** The SDK refreshes only reactively, when an upload gets a 401, on a
foreground `URLSession` (OpenWearablesHealthSDK 0.13.0, `OpenWearablesHealthSDK.swift`,
`attemptTokenRefresh`). One of its two triggers is the background-upload completion delegate
(`Internal/URLSessionDelegate.swift`), which hands the system completion handler back at once, after
which iOS may suspend the process. The request has already reached OW and committed; the reply
arrives on a socket the OS has torn down, the SDK sees a network error and keeps the old token. A
keychain write that fails silently (`Internal/Keychain.swift`, `save` ignores the delete result) is
a second, rarer path to the same state. The server cannot tell the two apart, and this design does
not need to.

**Evidence** (OW dev DB and `aca-ow-api-dev` console logs, queried 2026-09-11; logs retain 30 days):

| lockout | rotation whose reply was not applied | first refresh 401 | recovered by fresh mint | locked for |
| --- | --- | --- | --- | --- |
| user `541f2130` | 2026-08-20 09:53:27Z | +42 min | 2026-08-21 17:37:54Z | ~32 h |
| user `11de240a` | 2026-09-05 01:28:18Z | +3 h 33 min | 2026-09-05 07:53:53Z | ~6.4 h |
| user `11de240a` | 2026-09-11 15:10:04Z | +3 s | 2026-09-11 19:14:47Z | ~4.1 h |

The first-401 column is the time to the phone's next upload, not a property of the token; it comes
from log ingestion timestamps and is accurate to a few seconds.

Across all 1,317 SDK rotations since 2026-06-10, **63** successors were never used although a
later token exists for the same user and app (a lost reply and a chain abandoned by a reconnect look
identical here). Rotations that appear to have produced two successors from one token (about 42, a
timing inference, since nothing links the rows) are a separate hygiene defect of the unlocked
lookup; they are not the cause of any lockout, because either successor works.

On 2026-09-11 the user's missing workout arrived nine seconds after the manual reconnect, so the
SDK's HealthKit position had not advanced past the failed uploads; nothing was lost, only delayed
until a human intervened.

Sibling fix: fork PR #19 (merged 2026-08-03) closed the same class where OW is the *client* of a
rotating provider token (`backend/app/services/providers/templates/base_oauth.py:137`,
`backend/tests/integrations/test_oauth_refresh_race.py:63`). This document closes it where OW is the
*issuer*.

## 2. Scope

In scope — SDK refresh tokens (`token_type = sdk`) only. Every live SDK token on 2026-09-11 belongs
to the Calibra app, so in practice this is Calibra's Apple Health session. The choice of OW as the
place to fix it is D-01.

- `POST /api/v1/token/refresh` for SDK tokens: serialised, atomic rotation (D-06) and acceptance of a token
  whose rotation reply the client never applied (D-02, D-03, D-05, INV-01).
- `POST /api/v1/token/revoke` on a rotated SDK token (INV-03).
- `POST /api/v1/users/{id}/token`: a fresh mint revokes the user's earlier SDK chains for the same
  app (D-11).
- One migration adding the successor link (§5.1).
- Structured logs for refresh outcomes (D-08).
- The resulting acceptance-set change is enumerated in INV-05.

Explicitly out of scope (a reviewer may not raise findings against these):

- Cloud provider connections (WHOOP, Polar, Garmin, …). OW refreshes those tokens server to server
  (`backend/app/services/providers/templates/base_oauth.py:137`); fork PR #19 closed their correctness
  defect and zero provider connections have been revoked since it merged.
- Developer tokens, in every respect (D-04).
- The invitation-code mint path (`backend/app/services/user_invitation_code_service.py:75`). It mints
  under an `invite:` app id, so D-11 never reaches its tokens. Calibra does not use it: the app's only
  OW token path is calibra-app `lib/api/ow.ts` → the adapter, which reaches only `/users/{id}/token`
  (calibra-adapter `Calibra.Providers/OpenWearables/OpenWearablesClient.cs`).
- The Calibra app's handling of a dead session (showing "Needs reconnect" and a reconnect control).
  Separate spec in `calibra-app`. The SDK's `onAuthError` fires only on a 401–403 from a refresh or
  the post-refresh upload retry (`Internal/Outbox.swift`, `Internal/URLSessionDelegate.swift`); after
  this design that is a truthful "session dead" signal.
- Revoking a whole token family when a rotated token is replayed (D-07).
- Refresh-token expiry; none of the table's columns (`backend/app/models/refresh_token.py:17` through
  `backend/app/models/refresh_token.py:30`) is an expiry.
- Backfilling rows rotated before deploy (D-09).
- Any change to the iOS SDK.
- Populating `last_used_at` (`backend/app/models/refresh_token.py:29`); its only writer,
  `update_last_used` (`backend/app/repositories/refresh_token_repository.py:70`), has no callers in
  `backend/app`, and the column is null on every row.

## 3. Decisions

### D-01 — Fix at the issuer

**Context.** Three places could act: the iOS SDK, the Calibra adapter, or OW.

**Choice.** OW. It is the only party that sees both the rotation and the client presenting the
superseded token.

**Consequences.** One server change protects every SDK client without an app release. The
device-side mechanism (§1) stays unfixed; it cannot be fully fixed client-side, since no client can
guarantee it receives a reply.

<!-- depends: D-01 -->
**Why not the adapter.** It is not on the path. It mints the first pair once (calibra-adapter
`Calibra.Providers/OpenWearables/OpenWearablesClient.cs`, `/api/v1/users/{id}/token`) and
authenticates to OW with an API key; every later refresh goes from the SDK straight to OW. Putting
it on the path means proxying all SDK traffic.

<!-- depends: D-01 -->
**Why not the SDK.** It is third-party source vendored as a pod, and changing it needs an app
release that reaches only updated devices. It also cannot close the window: a reply can always be
lost between commit and receipt.

### D-02 — Grace hands back the existing successor; it never mints another

**Context.** When a superseded token arrives inside grace, OW could issue a new successor or return
the one it already issued.

**Choice.** Return the existing successor's id as `refresh_token`, with a freshly minted access
token. No new row, and no change to either row.

**Consequences.** A lineage has at most one live head (INV-02). A client that also loses the grace
reply can ask again and gets the same answer (INV-04).

<!-- depends: D-02, INV-02 -->
**Why not mint a new successor.** Every grace hit would leave an unrevoked token behind that nobody
holds — exactly the kind of orphan this design is removing, made on purpose.

### D-03 — Eligibility is keyed on the successor link, which only rotation writes

**Context.** Today a revoked token is a revoked token; OW cannot tell "superseded by a refresh" from
"revoked on purpose".

**Choice.** Add `rotated_from` on the successor (§5.1), written only by the rotation transaction.
A revoked token is eligible only if a row names it as `rotated_from` (INV-01). Explicit revocation,
D-11's revocation and rows rotated before deploy (D-09) produce no such row, so they stay final.

**Consequences.** No reason column is needed. One path remains by which an explicitly revoked token
could still unlock something — revoking a token *after* it was superseded — and INV-03 closes it.
`revoke_all_for_user` and `revoke_all_for_developer`
(`backend/app/repositories/refresh_token_repository.py:46`,
`backend/app/repositories/refresh_token_repository.py:58`) have no callers in `backend/app`; they
write no link either, so they stay final too.

### D-04 — SDK tokens only; the developer-token path is left byte-for-byte as it is

**Context.** `refresh_token` and `revoke_token` serve both token types.

**Choice.** Read the presented row without a lock, branch on `token_type`, and only in the SDK branch
open the serialised transaction of D-06. `token_type` is written only when a row is created
(`backend/app/services/refresh_token_service.py:43`, `backend/app/services/refresh_token_service.py:68`),
so the unlocked read cannot disagree with the re-read under the lock. Developer tokens take today's code unchanged: no grace, no lock,
no link, no revoke cascade.

**Consequences.** An SDK access token is rejected on every non-SDK route
(`backend/app/utils/auth.py:39`), and the SDK routes are two write-only POSTs
(`backend/app/api/routes/v1/sdk_sync.py:17`, `backend/app/api/routes/v1/sdk_logs.py:16`). So the
widened acceptance can at worst let one user's data be uploaded. A developer token reads everything
and belongs to an admin who can simply log in again.

<!-- depends: D-04, D-06, INV-05 -->
**Why not atomic rotation for developer tokens too.** Under D-06 without grace, the loser of two
concurrent developer refreshes would find the token revoked and get 401, where today it can get 200
with a forked successor. That would narrow the acceptance set on a path no incident motivates.

### D-05 — Grace lasts while the successor is unrevoked, capped at `sdk_refresh_grace_seconds`

**Context.** In the three confirmed lockouts the client presented the superseded token again after
about 3 s, 42 min and 3 h 33 min. Identity providers that pair rotation with a reuse window measure
it in seconds because their clients retry at once; this client retries on its next upload, which in
the evidence came 3 s, 42 min and 3 h 33 min later. A window of seconds would have saved one of the
three.

**Choice.** A superseded SDK token is eligible while its successor is unrevoked — neither rotated,
explicitly revoked, nor revoked by D-11 — **and** `now − t.revoked_at ≤ sdk_refresh_grace_seconds`
(the grace arm of INV-01).
For a rotated token, `t.revoked_at` is the rotation time. The setting is a positive integer number
of seconds, validated at startup: a non-positive or unparsable value fails startup, the way the
existing duration validator does (`backend/app/config.py:284`, evaluated at import by
`backend/app/config.py:357`). The default is Q-01.

**Consequences.** When the client applies the successor, its next refresh rotates the successor and
the superseded token loses grace at once. The cap only matters for chains that are never advanced.

<!-- depends: D-05, D-02, D-11, Q-01 -->
**Why a cap at all.** A chain that is never advanced keeps its unrevoked successor until D-11 revokes
it at the next reconnect, which may never come. Without a cap, the superseded token would stay
exchangeable for that successor for as long. That grants no new credential, since the successor is
live anyway, but it doubles the number of strings that unlock it, with no end date. The cap bounds
that.

### D-06 — Every SDK token transaction for one user and app is serialised by one lock

**Context.** Revoke and create commit separately and nothing serialises them, which is how one
presented token can produce two successors. Row locks cannot serialise a rotation against D-11's
revocation: a revoking `UPDATE … WHERE revoked_at IS NULL` cannot see a successor inserted after the
statement began, so it would leave that successor live.

**Choice.** Every SDK-branch transaction — refresh (INV-01), revoke (INV-03) and mint (D-11) — first
takes `pg_advisory_xact_lock(k)`, where
`k = hashtextextended(user_id::text || ':' || app_id, 0)` for the token's `(user_id, app_id)`.
PostgreSQL releases it at commit or rollback. Under the lock, rows are read with plain `SELECT`s:
every path that updates or links that user's and app's SDK tokens holds the same lock, so nothing a
transaction reads can change under it, except through the user-delete cascade (§5.2). The
out-of-scope invitation-code mint (§2) takes no lock, but all it writes is a new root row — its
`rotated_from` null and its id not yet held by any client — through the same insert every mint uses
(`backend/app/services/refresh_token_service.py:29`), so no locked transaction can have read that
row. Once issued, an invitation-code token is an SDK token like any other, and refreshing it takes
the lock. Rotation revokes the presented token, inserts the successor with
`rotated_from = t.id`, and commits once.

**Consequences.** Refreshes, revokes and mints for one phone run one at a time. The loser of two
concurrent refreshes waits, then finds the token rotated with an unrevoked successor, and takes the
grace path. The unique `rotated_from` (§5.1) makes a second successor a database error even if the
lock were missing (INV-02). A hash collision between two pairs only makes unrelated requests wait for
each other. The user-delete cascade takes no advisory lock and can still conflict (§5.2). Developer
tokens take no lock (D-04).

### D-07 — A deliberate departure from upstream's "reuse means theft" framing

**Context.** Upstream lists "detects theft: if legitimate user's refresh fails (token already used),
indicates compromise" among rotation's benefits (`.ai/specs/002-refresh-token.mdx:144`). It
implements no detection: a revoked token gets the same 401 as an unknown one
(`backend/app/services/refresh_token_service.py:96`). The OAuth security BCP pairs rotation with
family revocation on reuse.

**Choice.** Treat a superseded SDK token presented while its successor is unrevoked as a client that
missed the reply, not as theft. Add no family revocation.

**Consequences.** Compared with today this is neutral on containment and stronger on detection.
Today a thief holding token `T` wins by refreshing before the device does, and `T` never expires and
is never revoked. Under this design the only new acceptance is "`T` after rotation, while `T1` is
unrevoked": the thief receives `T1`, the credential the device is about to hold and one they could
already obtain by being first. Once either party rotates `T1`, the other holds a dead token, as
today. What is new is that "`T` presented after `T1` was used" — the actual reuse signature — becomes
a logged, per-user event (D-08) where today it is a bare 401.

<!-- depends: D-07, D-04 -->
**Why not family revocation.** Every observed "reuse" here was the legitimate device. Revoking the
family would have turned each of the three lockouts into the same lockout, reached faster. Blast
radius: D-04.

### D-08 — Observability names its consumer

**Context.** Today nothing at INFO identifies the user on a refresh. The service's own line is
debug-level (`backend/app/services/refresh_token_service.py:118`) while the app logs at INFO
(`backend/app/main.py:26`), and the access-log line carries only the path and status, as observed in
the §1 logs.

**Choice.** On `POST /api/v1/token/refresh`, for SDK tokens and for ids that match no row, every
request that returns 200 or 401 emits exactly one line through `log_structured`
(`backend/app/utils/structured_logging.py:31`, which prints bare JSON) at level `info`, with
`message` equal to the `action`. Each line carries `action`, plus `user_id` and `token_type`
whenever the first, unlocked read found a row — including a row deleted before the re-read under
the lock, which is then rejected as `unknown` with its `user_id`:

- `action = "refresh_token_rotated"`
- `action = "refresh_token_grace_reissued"`, with `rotated_age_seconds` (integer, seconds rounded
  down)
- `action = "refresh_token_rejected"`, with one `reason`: `unknown`, `revoked`,
  `rotated_successor_used`, `rotated_past_grace`

A request that ends in an unhandled error — a 500 from §5.2's user-delete overlap, or a database outage — emits no
D-08 line; its trace is the existing access-log line with status 500. `POST /api/v1/token/revoke`,
the developer branch and the mint route emit nothing new.

**Consequences.** The query in §5.6 answers "which SDK clients are stuck right now". It is committed
as `docs/superpowers/specs/queries/2026-09-11-sdk-refresh-stuck-clients.kql`, and a test reads that
file and asserts the three `action` strings and the three `reason` strings it filters on are the
literals the code emits, and that the code's full set of reasons is exactly those three plus
`unknown` — so a rename on either side, or a new reason the query does not know, goes red.
Another test asserts that the emitted line parses as JSON with those fields.

### D-09 — No backfill

**Context.** Rows rotated before deploy have no successor row naming them.

**Choice.** They stay ineligible (`revoked`).

**Consequences.** A client already locked out at deploy time still needs one manual reconnect.

<!-- depends: D-09, D-02, D-03 -->
**Why not backfill.** Pairing by "successor created within 2 s of the revocation" is a heuristic,
ambiguous whenever a user has several chains, and a wrong pairing would hand one chain's successor
to another's holder.

### D-10 — Ship on the fork's release line

**Context.** The fork is deployed from a tag pinned in the deploy repo.

**Choice.** Merge into `release/0.6.2-syn`, tag `0.6.2-syn.7`, and point `OW_REF` at the new tag in
calibra-ow-deploy `.github/workflows/deploy-openwearables.yml`. A push to that repo's `main` deploys
dev. Nothing is reported upstream.

**Consequences.** The migration (§5.1) runs at API start (`backend/scripts/start/app.sh:10`); rollback
is an image rollback.

### D-11 — A fresh mint revokes the user's earlier SDK chains for the same app

**Context.** A fresh mint never revokes anything (`backend/app/api/routes/v1/sdk_token.py:71`), so
every reconnect leaves the previous chain live: one test user held 72 live SDK refresh tokens on
2026-09-11. Calibra is one phone per account (Dragan, 2026-09-11).

**Choice.** `POST /api/v1/users/{id}/token` takes D-06's lock for `(user_id, app_id)`, revokes every
unrevoked SDK refresh token with that `(user_id, app_id)`, and creates the new one, in one
transaction. Those revocations write no `rotated_from` row, so the revoked tokens are final (D-03).

**Consequences.** A token held by anyone other than the current phone dies the moment the user
reconnects, which is stronger than today. A second device signed into the same account loses its
Apple Health session whenever the other reconnects; that device would then show "Needs reconnect".
Scoping to `app_id` leaves the invitation-code tokens (§2) untouched.

<!-- depends: D-11, D-05 -->
**Why revoke on mint rather than expire idle tokens.** Expiry needs `last_used_at`, whose only
writer has no callers (§2), and a clock the SDK's reactive refresh does not follow. A reconnect is an explicit user act
that says "this phone is the one", and one phone per account makes that the whole truth.

## 4. Invariants

### INV-01 — The acceptance predicate for `POST /api/v1/token/refresh`

For a presented string `p` at time `now`, with `grace = sdk_refresh_grace_seconds`:

```
t := row with id = p                                 -- absent ⇒ reject(unknown)
if t.token_type != sdk:                              today's code, unchanged (D-04)
begin transaction
pg_advisory_xact_lock(k(t.user_id, t.app_id))        -- D-06
t := row with id = p                                 -- re-read; absent ⇒ reject(unknown)
if t.revoked_at is null:                             rotate(t)
else:
    s := row with rotated_from = t.id                -- absent ⇒ reject(revoked)   (D-03)
    if s.revoked_at is not null:                     reject(rotated_successor_used)
    elif now − t.revoked_at > grace:                 reject(rotated_past_grace)   (D-05)
    else:                                            grace(t, s)        -- INV-04, no write
```

`rotate(t)` sets `t.revoked_at = now`, inserts `s` with `rotated_from = t.id` and the user and app of
`t`, commits, and returns today's `TokenResponse` shape
(`backend/app/services/refresh_token_service.py:132`) with `refresh_token = s.id`. Boundary:
`now − t.revoked_at = grace` is accepted. Every `reject(...)` is today's 401 with today's body
(`backend/app/services/refresh_token_service.py:97`); only the log line (D-08) distinguishes them.
The re-read exists because the first read happened before the lock; the successor condition is
evaluated exactly once, under the lock.

### INV-02 — At most one successor per SDK token

`rotated_from` is unique (§5.1). Two rows naming the same predecessor are a database error, so a
token cannot have two successors regardless of the lock. The lock (D-06) turns what would be an
error for the loser into the grace path.

### INV-03 — The predicate for `POST /api/v1/token/revoke`

```
t := row with id = p                                 -- absent ⇒ 404
if t.token_type != sdk:                              today's code, unchanged (D-04)
begin transaction
pg_advisory_xact_lock(k(t.user_id, t.app_id))        -- D-06
t := row with id = p                                 -- re-read; absent ⇒ 404
if t.revoked_at is null:                             revoke(t); 204      -- as today
else:
    s := row with rotated_from = t.id                -- absent ⇒ 404     -- as today
    if s.revoked_at is not null:                     404                 -- as today
    else:                                            revoke(s); 204      -- new
```

`revoke(x)` sets `x.revoked_at = now` and writes no link. The age cap does not apply here. Without
the new branch, a client that missed a rotation reply and then logged out would leave its successor
live and reachable through grace.

### INV-04 — Grace is idempotent and writes no refresh-token rows

`grace(t, s)` returns today's `TokenResponse` shape with `refresh_token = s.id`, a new access token
for `(s.user_id, s.app_id)`, and `expires_in` as today
(`backend/app/services/refresh_token_service.py:136`). It inserts no row and updates neither `t`
nor `s`. Repeating it returns the same `refresh_token`.

### INV-05 — The acceptance set only widens, except by D-11

<!-- depends: D-02, D-03, D-04, D-05, D-06, D-09, D-11, INV-01, INV-02, INV-03, INV-04 -->
| request (SDK tokens unless stated) | today | this design |
| --- | --- | --- |
| refresh: unknown id, or user deleted (rows cascade, `backend/app/mappings.py:42`) | 401 | 401 |
| refresh overlapping the deletion of its own user | 401, 200 or 500 by timing | 401, 200 or 500 by timing (§5.2) |
| refresh: live token | 200, rotates | 200, rotates atomically |
| refresh: two concurrent refreshes of one live token | both 200 with two successors, or the loser 401, depending on timing | both 200, **one successor** |
| refresh: revoked explicitly, by D-11, or rotated before deploy | 401 | 401 |
| refresh: rotated, successor unrevoked, within the cap | 401 | **200, same successor** |
| refresh: rotated, successor revoked | 401 | 401 |
| refresh: rotated, past the cap | 401 | 401 |
| revoke: live token | 204 | 204 |
| revoke: unknown id, or revoked token with no successor | 404 | 404 |
| revoke: rotated, successor unrevoked | 404, no effect | **204, successor revoked** |
| revoke: rotated, successor revoked | 404 | 404 |
| mint for `(user, app)` | 200, older chains stay live | 200, **older chains for that `(user, app)` revoked** |
| any developer-token request | today | unchanged (D-04) |

The mint row is the one narrowing, and it is D-11's purpose. Every other row widens or is unchanged.

## 5. Design

### 5.1 Schema

One nullable column on `refresh_token`, added by one Alembic migration (migrations run at API start,
`backend/scripts/start/app.sh:10`):

- `rotated_from`: `str_64` (`backend/app/mappings.py:31`), foreign key to `refresh_token.id` with
  `ON DELETE SET NULL`, **unique** (INV-02), null on every row minted rather than rotated.

<!-- depends: INV-01, INV-02 -->
A set link always points at an existing row. No code in `backend/app` deletes refresh-token rows
(grep, 2026-09-11); they disappear only when their user is deleted, through the cascade at
`backend/app/mappings.py:42`, which takes predecessor and successor together.

<!-- depends: D-09, D-10, INV-01 -->
The column is nullable and the current code ignores it, so rolling the image back is safe. Rows
written during a rollback carry no link and fall under D-09.

### 5.2 Flow

`refresh_token` and `revoke_token` each read the presented row unlocked, branch on `token_type`
(D-04), and in the SDK branch run INV-01 or INV-03 as one transaction under D-06's lock. The mint
route takes the same lock, then runs D-11's revocation and the insert in one transaction. The
repository gains one method per transaction. The commit-per-call helpers
(`backend/app/repositories/refresh_token_repository.py:20`,
`backend/app/repositories/refresh_token_repository.py:42`) remain for developer tokens.

<!-- depends: D-06, D-08, INV-01 -->
**Deadlock with user deletion.** `DELETE /users/{id}` (`backend/app/api/routes/v1/users.py:62`)
takes no advisory lock. It locks the user row and cascades through the user's refresh-token rows,
while a rotation holding D-06's lock updates one of those rows and inserts a successor whose foreign
key needs the user row. If a refresh overlaps the deletion of that same user, the refresh can end
three ways: 401 or 200 if one side finishes first; a 500 if PostgreSQL aborts it as the deadlock
victim; or a 500 if the deletion commits between its re-read and its write, which then hits a
missing row or a foreign-key violation. Every 500 emits no D-08 line. The account is being deleted;
this is accepted and not engineered around.

### 5.3 Interleavings (SDK tokens)

<!-- depends: D-02, D-05, D-06, D-11, INV-01, INV-02, INV-03, INV-04 -->
Every SDK operation below holds D-06's lock for the same `(user, app)`, so each pair runs in one
order or the other; the table gives both.

| A | B | outcome |
| --- | --- | --- |
| refresh(T0 live) | refresh(T0 live) | A rotates T0 to T1 and commits. B then re-reads T0, finds it rotated with T1 unrevoked, and returns T1 through grace. One successor. |
| refresh(T0, grace) | refresh(T1) | If B runs first, T1 is rotated to T2 and A rejects (`rotated_successor_used`). If A runs first, it returns T1; B then rotates T1 to T2, and the client gets T2 on its next refresh. |
| refresh(T0 live) | revoke(T0) | If revoke runs first, A rejects (`revoked`). If refresh runs first, T0 is rotated and revoke then revokes T1 (INV-03), so any later T0 or T1 is rejected. |
| refresh(T1) | revoke(T0) | If refresh runs first, T1 is rotated and revoke(T0) answers 404 as today. If revoke runs first, T1 is revoked and the refresh rejects (`revoked`). |
| refresh(T0 live) | mint | If refresh runs first, T1 exists when the mint revokes, so T1 is revoked and only M is live. If mint runs first, T0 is revoked with no successor and the refresh rejects (`revoked`). Either way the phone that reconnected holds M. |
| refresh(T0, grace) | mint | If mint runs first, T1 is revoked and A rejects (`rotated_successor_used`). If A runs first, it returns T1, which the mint then revokes; the next refresh with T1 is rejected (`revoked`) and the app shows "Needs reconnect" — the phone that reconnected holds M. |
| any | delete user | Not serialised by the lock. Rows cascade away and the next call is `unknown`; a refresh overlapping the delete ends in 401, 200 or 500 (§5.2). |

### 5.4 Tests (TDD) and the mutation that must redden each

<!-- depends: D-02, D-03, D-04, D-05, D-06, D-08, D-11, INV-01, INV-02, INV-03, INV-04 -->
| test | mutation that must turn it red |
| --- | --- |
| superseded token, successor unrevoked → 200 with the same `refresh_token` | delete the grace branch |
| refresh where the user is deleted on a second connection between the unlocked read and the lock (barrier) → 401, logged as `unknown` with the row's `user_id` | assume the row is present after the re-read (the request raises → 500), or drop `user_id` on this path |
| grace twice → same `refresh_token`, row count unchanged | mint a new successor in grace |
| superseded token after its successor was rotated → 401 | drop the successor-unrevoked condition |
| superseded token after its successor was explicitly revoked → 401 | same mutation |
| superseded token after its successor was revoked by a mint → 401 | same mutation |
| token explicitly revoked while live (existing `backend/tests/api/v1/test_token.py:94`) → 401 | let "no successor row" fall through to the grace branch |
| superseded token at exactly the cap → 200; one second past → 401 (injected clock) | drop the age check (second case), or make it `≥` (first case) |
| settings: `sdk_refresh_grace_seconds = 0` and `"7d"` each fail startup | drop the validator |
| `/token/revoke` on a rotated token whose successor is unrevoked → 204, successor revoked, superseded token then 401 | remove INV-03's new branch |
| `/token/revoke` on a rotated token whose successor is revoked → 404 | revoke unconditionally and answer 204 |
| `/token/revoke` where the user is deleted on a second connection between the unlocked read and the lock → 404 | assume the row is present after the re-read: the request raises → 500 |
| revoke(T0) against refresh(T0 live) on two real connections, the refresh paused after inserting T1 and before commit while the revoke starts → afterwards T0 and T1 are both revoked | drop the advisory lock from revoke: it reads T0 as live, revokes only T0, and T1 stays live |
| rotation inserts the successor with `rotated_from` = the presented id | stop writing the link (every grace test also reddens) |
| two concurrent refreshes of one live token on two real connections, A paused after inserting T1 and before commit while B starts → both 200, one successor | drop the advisory lock from refresh: B reads T0 as live, inserts a second row with `rotated_from = T0`, and fails on the unique index once A commits → 500 |
| refresh(T0) in grace against refresh(T1) on two real connections, B paused after revoking T1 and before commit while A starts → A waits, then 401 | drop the advisory lock from refresh: A reads T1 as still unrevoked and returns 200 with a token B is revoking |
| refresh(T0 live) against a mint on two real connections, the refresh paused after inserting T1 and before commit while the mint starts → afterwards only M is live | drop the advisory lock from the mint: T1 stays live |
| migration: a second row with the same `rotated_from` is rejected by the database | drop the unique constraint |
| mint for `(user, app)` revokes that user's earlier live SDK tokens for that app, leaves other apps' tokens live, and the developer path untouched | drop D-11's revocation, or drop the `app_id` filter |
| each refresh outcome emits one JSON line that parses, with the D-08 fields; the `action`/`reason` strings equal the literals read from the committed `.kql` file, and the code's reason set is exactly those plus `unknown` (D-08) | rename one string in the code or in the `.kql` file, add a reason, or emit through the plain logger |
| a refresh whose transaction raises (injected database error) emits no D-08 line and returns 500 | emit a D-08 line from the error path |
| `/token/revoke`, a mint, and a developer-token refresh each emit no D-08 line | emit a D-08 line from any of them |
| developer token: rotate, then present the old one → 401; two concurrent developer refreshes → as today; revoke a rotated developer token → 404 | apply the SDK branch to developer tokens |

<!-- depends: INV-01, INV-03, INV-05, D-04 -->
These existing tests now run through the new SDK code without a change in outcome, so they stay as
regression pins and the implementation must keep them green:

- refresh of an unknown id (`backend/tests/api/v1/test_token.py:82`);
- revoke of a live token (`backend/tests/api/v1/test_token.py:156`);
- revoke of an unknown id (`backend/tests/api/v1/test_token.py:178`);
- revoke of an already-revoked token (`backend/tests/api/v1/test_token.py:189`).

<!-- depends: D-02, D-04, D-05 -->
**Contract change to an existing test.** `test_refresh_token_rotation_invalidates_old_token`
(`backend/tests/api/v1/test_token.py:122`) asserts upstream's rule: the old token is 401 straight
after rotation. Under D-05 that step now returns 200 with the same successor. The test is rewritten
to refresh with the new token first and then assert the old one is 401. That keeps its intent,
"rotation invalidates", at the point where it now applies. The developer-token row above adds the
original assertion for developer tokens, where it still holds. `test_refresh_revokes_old_token`
(`backend/tests/services/test_refresh_token_service.py:144`) still holds and gains an assertion on
the successor's `rotated_from`.

<!-- depends: D-06, INV-02 -->
**Why the concurrency tests need two real connections.** The `db` fixture runs each test inside one
connection and a savepoint (`backend/tests/conftest.py:101`). A single connection cannot contend with
itself for a lock, so it cannot show D-06's advisory lock doing anything. Each concurrency test opens
two connections and pauses one side at the point its row names. The paused side resumes and commits
only once the other request is either observed waiting on a lock
(`pg_stat_activity.wait_event_type = 'Lock'` for its backend) or has returned — the first with the
lock in place, the second under the mutation — so neither outcome depends on timing. Each test
cleans up explicitly.

### 5.5 Rollout

Per D-10:

1. Open a PR into `release/0.6.2-syn`.
2. Tag `0.6.2-syn.7`.
3. Bump `OW_REF`.
4. Verify on dev:
   - new SDK rotations insert a successor carrying `rotated_from`;
   - a reconnect from the app leaves exactly one live SDK token for that user and app;
   - after the two internal test users (`11de240a`, `541f2130`) have each had one successful refresh
     post-deploy, the §5.6 query returns no row for either.

### 5.6 The consumer query

Committed as `docs/superpowers/specs/queries/2026-09-11-sdk-refresh-stuck-clients.kql` (D-08); this
copy is illustrative and the file is authoritative.

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == 'aca-ow-api-dev'
| where Log_s has 'refresh_token_rotated' or Log_s has 'refresh_token_grace_reissued'
     or Log_s has 'refresh_token_rejected'
| extend j = parse_json(Log_s)
| extend action = tostring(j.action), reason = tostring(j.reason), user_id = tostring(j.user_id)
| summarize last_ok = maxif(TimeGenerated, action in ('refresh_token_rotated', 'refresh_token_grace_reissued')),
            last_rejected = maxif(TimeGenerated, action == 'refresh_token_rejected'
                                  and reason in ('revoked', 'rotated_successor_used', 'rotated_past_grace')),
            rejections = countif(action == 'refresh_token_rejected')
    by user_id
| where isnotempty(user_id) and last_rejected > coalesce(last_ok, datetime(1970-01-01))
| order by last_rejected desc
```

<!-- depends: D-07, D-08, D-11, INV-01 -->
Each row is a user whose latest refresh outcome is a rejection. After a reconnect the row clears at
that phone's next successful refresh: its first upload after the new access token expires
(`backend/app/config.py:64`), which in the §1 evidence was up to 3 h 33 min later. The query's
reason filter leaves out `unknown`, whether or not the line carries a `user_id` (D-08). A user with
`rotated_successor_used` and no later success is either this phone after a reconnect (D-11) or the
reuse signature of D-07.

<!-- depends: D-02, D-05, D-06, INV-01, INV-04 -->
**Example — 2026-09-11 under this design.**
- **15:10:04:** the SDK refreshes with `T0`, and OW rotates it to `T1`. The reply is not applied on
  the device.
- **About 3 s later:** the SDK presents `T0` again. A row names `T0` as `rotated_from`, `T1` is
  unrevoked, and the age is under the cap, so OW returns `T1` with a new access token.
- **Result:** the upload retry succeeds. The next refresh — at the phone's first upload after the
  access token expires (`backend/app/config.py:64`) — rotates `T1`. No 401 reaches the app, the
  workout lands within minutes instead of ~4.1 h, and nobody reconnects.

<!-- depends: D-05, INV-01, Q-01 -->
**Example — 2026-08-20.** The superseded token was presented 42 minutes after rotation. Any cap of
42 minutes or more (the boundary is inclusive) turns a ~32 h lockout into a 42-minute delay.

## 6. Open questions

### Q-01 — The default for `sdk_refresh_grace_seconds`

<!-- depends: D-05, D-11 -->
The longest observed gap between a rotation and the next presentation was 3 h 33 min. The
recommendation is **604800 (7 days)**: about 47× the longest gap, enough for a phone left idle over
a weekend, while still bounding chains that are never advanced (D-05). With D-11, a reconnect ends
such chains sooner in practice.

**Resolved 2026-09-11 (Dragan): 604800 seconds (7 days).**

## 7. Change log

| Date | Change | Type | Sweep report |
| --- | --- | --- | --- |
| 2026-09-11 | Initial draft, revised in the same session before acceptance. Final shape follows an independent analysis: successor link on the successor row (`rotated_from`, unique); token-type branch before any lock; `log_structured` at INFO; validated grace setting; D-11 added on Dragan's "one phone per account"; the two-successor forks reclassified as hygiene, not cause; upstream reporting removed. Design-review round 2: row locks replaced by one per-`(user, app)` advisory lock so a mint cannot miss a concurrently inserted successor; D-08 defines the 500 path and the deleted-between-reads path. Design-review round 3 corrected claims only, with no change to design behaviour: concurrency test interleaving pinned; user-delete overlap outcomes completed. | semantic (round 2, before acceptance) | — |
