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
  from `OW_REPO = Synpatik-GmbH/open-wearables` at the tag in `OW_REF` (currently `0.5.2-syn.7`).
- Deploys always reference an **immutable tag**, never a branch head.

## Current base

- Upstream base: **0.5.2** (`a2060c7`)
- Target upgrade: **0.6.2** (`a07818f`)

---

## The durable delta (what must survive every upgrade)

These are genuinely Synaptik-specific and will never be upstreamed. **The reconciliation
checklist for any upgrade is: confirm each of these still applies and still behaves.**

| Feature | Commits | Files | Why it's ours | Notes |
|---|---|---|---|---|
| **Edwards HR-zone payload** on `workout.created` | `feeabca`, `29c0a35`/`ff312ff` (order fix) | `event_record_service.py`, `heart_rate.py`, `outgoing_webhooks/events.py` | **The .NET adapter depends on this** — its `WorkoutHrZoneHealService` replicates "0.5.2-syn.6's EXACT Edwards algorithm". Dropping/changing it breaks parity. | ⚠️ HIGHEST-RISK item on any upgrade. Verify payload shape against the adapter after upstream zone changes (0.6.2 adds `b0ff3d8` HR/Power zones, `111a952` Oura zone_offset). |
| **Webhook fast lane** | `01e6eae`, `0905a69` | `outgoing_webhooks/events.py`, `scripts/start/worker.sh` | Latency requirement: priority events routed to a dedicated `webhook_sync` Celery queue (`CELERY_QUEUES` override). Consumed by `aca-ow-worker-priority-dev`. | Keep `worker.sh` CELERY_QUEUES override. |
| **Polar Recharge bridges** | `753ec9c` (HRV→`rmssd`), `b2e93f9` (RHR→`resting_heart_rate`) | `services/providers/polar/data_247.py` | Signal mapping Calibra needs; not in upstream. | |
| **WHOOP `read:profile` scope** | part of `3e287c0` | `config.py` | Our WHOOP app registration needs it. | Split from the Redis-TLS half of the same commit (see below). |
| **respiratory_rate → data_point_series** (Thread 14f) | `513b94d` | `data_point_series_repository.py`, sleep save path | Sleep-side RR persistence. | Upstream `eddf5d9` #1235 is Garmin-*side* RR — likely complementary; **verify not superseded**. |

---

## Superseded by upstream — DROP on the 0.6.2 upgrade

Upstream reimplemented these independently. Do not carry them forward.

| Your commit | Superseded by | Action |
|---|---|---|
| `9c82908` / `23ad5ea` Polar TL/distance → float | `a10227b` #1204 | Drop (merges clean but duplicates upstream — delete). |
| Redis-TLS half of `3e287c0` | `a764df2` #1134 | Take upstream TLS; re-add only the WHOOP scope line. |
| `d09028c` snapshot ORM attrs before `after_commit` | `e8efeea` #1208 (fires webhook directly, no after_commit) | Skip — the mechanism it patched no longer exists. |
| `d436010` cache `last_synced_at` | `ac351ab` #1147 + `f5b9696` #1215 sync refactor | Reconcile — likely skip if the refactor avoids the expired-session lazy-load. |

## Noise — squash/collapse before rebasing

- Ruff/style-only: `26faed1`, `7a923b2`, `5c33af7`, `94fd4f8` → fold into parents.
- Duplicated commits: `35cbdc0` == `c38cf33`; `29c0a35` == `ff312ff` → collapse.

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
