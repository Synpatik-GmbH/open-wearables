# 2.96.6 No lost wearable data

Spec v0.6, Product Studio, 14 Oct 2026. Roadmap item 2.96.6 (parent 2.96 Wearable integrations at launch), build child 2.96.6.1.
Status: AGREED, ready for build. Predicates agreed by Andrei on 13 Oct 2026 (V6, Q5, N9, L1, L2); N9 scope to every source, R1, R2, the 24-hour late-data bound and the 22-hour retry bound agreed 14 Oct 2026. Data-protection ruling R-2026-10-13-e decided 13 Oct 2026.
Source: Andrei's account on 13 Oct 2026 (no night data before and after the account was re-created at about 13:05 Berlin; a Polar activity at a time later than the screen's clock), the CC read-only trace relayed 13 Oct 2026, 13:43 (code paths only, no stored rows checked), the CC build plan relayed 14 Oct 2026, 08:23, the CC build report relayed 14 Oct 2026, 14:38 (PRs OW fork #40 and adapter #433 open; Polar call volume), the CC R1 report relayed 14 Oct 2026, 14:50, and the CC review report relayed 14 Oct 2026, 19:57.
Order: first of the four items from the 13 Oct walkthrough. 2.96.7.1 starts after this item's PRs merge (both touch the connection record). 2.97.8 (P18, the 28-day backfill at connect) relies on nights arriving.
Changes from v0.5 (Andrei, 14 Oct 2026, on Codex finding 4271871228): a failed fetch is retried at growing intervals, at least once every 22 hours, instead of on the very next sync; the newest night is still requested on every pull (3.2 cause 3, AC4).
Changes from v0.4 (Andrei, 14 Oct 2026, on the CC R1 report): predicate R2 added: when a Polar member opens the app and last night is not stored, Calibra asks Polar then, and readiness shows within 1 minute of opening if Polar has the night (3.1, AC18). A background pull schedule that delays a new night by up to 60 minutes is accepted only together with R2.
Changes from v0.3 (Andrei, 14 Oct 2026, on the CC build report): predicate R1 added (Polar calls within Polar's daily limit for 50 Polar members, AC17); late data stored within 24 hours of being served (3.1, cause 2, AC2); design-review readings confirmed: the 90-day window includes day 90, the definition of a shifted activity, of a failed Polar registration and of missing data; stored non-Polar activities that crossed midnight are not re-dated.
Changes from v0.2 (Andrei, 14 Oct 2026, on the CC plan): activities from every source count on the Berlin day they started (3.4, AC13); section 4 references corrected per the plan; three non-goals added (Polar continuous heart rate and daily-activity start times, heart-rate zone minutes on shifted workouts, the app showing activity times in Berlin time); data-protection condition on operational records added (section 9).
Changes from v0.1: L1 and L2 agreed; acceptance criterion 16 added (each shifted activity corrected exactly once); data-protection ruling decided as R-2026-10-13-e.

## 1. Intent

**Who.** Every member with a wearable, first Polar, then every source.

**Pain.** Readiness is built from nights. Per the trace, a night can be lost in eight places Calibra controls, and none of them tells the member. The noon cutoff alone means a member who connects after 12:00 Berlin may never get last night. Separately, Polar activity times are shifted by the time-zone offset in the wrong direction, so a late-evening workout lands on the next day and the Movement usual is built on the wrong days.

**Why now.** The founding member's first morning depends on last night arriving. 2.97.8's P18 tells the member to open their wearable's app and sync; that advice only works if a late night is still fetched.

**What this slice delivers.** No night, activity or reading is lost because of something Calibra controls, and every activity carries its true start time.

## 2. Definitions

- **Data:** a night (sleep, Nightly Recharge, overnight heart rate, HRV), an activity, or a daily reading, from any connected source.
- **Available at the source:** the source serves the data to Calibra through its API (Polar) or the member's device has written it (Apple Health).
- **Lost:** available at the source, within the window of Q5, and never stored by Calibra.
- **Outside Calibra's control:** data the source never serves, data recorded before the source allows Calibra to read it, and data a member has not consented to share.

## 3. Behaviour

### 3.1 The rule (V6)

- Data available at the source within the last 90 days (Q5) is fetched and stored within 24 hours of becoming available.
- A date exactly 90 days old is inside the window.
- Missing data for a night means its sleep is missing, or its HRV from the member's main HRV source is missing.
- Calibra's Polar calls stay within Polar's daily limit with 50 Polar members connected (R1). A fetch refused by the limit is a failed fetch (cause 3), not a lost one.
- When a Polar member opens the app and last night is not stored, Calibra asks Polar for it at that moment. If Polar has the night, readiness shows within 1 minute of opening (R2). The background schedule continues for members who do not open the app. A night the watch has not yet synced to Polar is outside Calibra's control.

### 3.2 The eight causes from the trace, each closed

1. **Noon cutoff.** A date still missing data after 12:00 Berlin keeps being requested until its data arrives or it is older than 90 days. Last night is always requested for a new connection, whatever the time of connection.
2. **Late data.** Data that reaches the source after its date has passed is fetched within 24 hours of reaching it, up to 90 days after its date.
3. **Failed fetch.** A failed fetch for a date or data type is retried at growing intervals, at least once every 22 hours, until it succeeds or the date is older than 90 days; the newest night is still requested on every pull; the sync never moves past a date whose fetch failed. A failure is logged as a code, not free text (R-2026-10-06-a, R-2026-10-06-b).
4. **Polar registration.** A registration has failed when Polar answers anything other than success (2xx) or already registered (409), does not answer, or no Polar user id is held. A failed registration is retried. Until it succeeds, Settings > Connections shows Polar as "Not connected" with Connect (L1). The member is never shown Polar as Connected while registration has not succeeded.
5. **Connection record.** The connection record that decides which sources are requested is written when the member connects, not when the app first lists connections.
6. **HRV source.** A member with exactly one HRV source has it used without choosing; their HRV is stored. Choosing between several sources stays parked.
7. **Consent and account link.** Data that arrives before the member's account link exists is fetched again once the link exists. Data for a member without the required consent is not stored (invariant).
8. **Nightly Recharge by pull only.** Covered by 1 to 3: Recharge is requested on every sync for every date still missing it inside the window.

### 3.3 Other sources

- The same eight checks apply to every connected source. The plan names, for Apple Health, which of the eight can occur and closes each one that can.

### 3.4 Activity start time (N9, L2)

- Every activity is stored with its true start instant. Polar's local start time is converted to UTC by subtracting its offset.
- Every activity, from every source, counts on the Berlin day it started, including one that ends after midnight (agreed 14 Oct 2026).
- Activities already stored with a shifted time are corrected in place to their true start instant, each exactly once (L2). Shifted means: in Open Wearables, a Polar workout with no record that its time is true; in the adapter, a row whose start equals the Open Wearables start plus twice the offset.
- Stored activities from other sources that crossed midnight keep their stored day; only new or re-ingested activities count on their start day. The Movement usual uses the corrected data from the day the correction runs. Stored states of past days and cards already sent are not changed.
- The plan confirms that the true start of every stored shifted activity can be recomputed. If any cannot, the build stops at the plan and L2 reopens with Andrei before any stored activity is changed.

### 3.5 Finding what hit Andrei's account

- The plan's first step names which of the eight causes stopped night data for the account created 13 Oct 2026 at about 13:05 Berlin, from logs CC can read. If no readable source shows it, the plan says what is needed and from whom.

## 4. Affected surfaces

From the CC read-only trace relayed 13 Oct 2026, 13:43, corrected by the CC plan relayed 14 Oct 2026, 08:23.
- Open Wearables fork: `backend/app/api/routes/v1/oauth.py:113-130` (historical sync on connect), `backend/app/services/providers/polar/oauth.py:32-57` (registration), `backend/app/services/providers/polar/data_247.py:157-173`, `:482-490`, `:997-1084` (sleep and Recharge), `backend/app/services/providers/polar/workouts.py:34-45` (no date filter), `:69-71` (offset added instead of subtracted), `app/integrations/celery/tasks/sync_vendor_data_task.py:498-499` (failure skipped, cursor advances), `data_247.py:1065-1082` (per-type errors swallowed), `backend/app/schemas/providers/polar/webhook.py:6-14` (no Recharge webhook).
- Adapter: `ReconcileCutoff.cs:59` (noon cutoff constant), `PhysiologyReconcileOrchestrator.cs:212-215` (a complete or finalized day is never pulled again), `PhysiologyReconcileService.cs:180` (2-day force sync), `NightIngestService.cs:96-113` and `:159-172` (writes; HRV source gate), `WearableConnectEndpoints.cs:270-295` (connection record), `OwWebhookEndpoints.cs:231-261` (consent and mapping).
- Settings > Connections, the Polar row state (L1): served by the adapter; no calibra-app change (CC plan, 14 Oct 2026).
- Apple Health path: as named in the plan (3.3), including the Open Wearables import retry.

## 5. Acceptance criteria

1. A member who connects Polar after 12:00 Berlin gets last night stored once Polar serves it.
2. A night that Polar first serves 1 to 90 days after its date is stored within 24 hours of being served.
3. A night or activity older than 90 days when first served is not fetched by the hourly sync.
4. When a fetch for a date fails, that date is requested again at least once every 22 hours, and the newest night is still requested on every pull; no later date is marked synced past it until it succeeds or turns 90 days old.
5. A failure record holds a code and no free-text error message.
6. A failed Polar registration is retried; until it succeeds, Settings > Connections shows Polar as "Not connected" with Connect, and never as Connected (L1).
7. The connection record exists from the moment the member completes the connection, before the app lists connections.
8. A member whose only HRV source is Polar has Polar HRV stored every night it is served, without choosing a source.
9. Data that arrived before the member's account link existed is stored after the link is created.
10. Data for a member without the required consent is not stored.
11. Nightly Recharge for a date missing it is requested on every sync while the date is inside the 90-day window.
12. A Polar activity recorded at 10:25 Berlin summer time is stored at 08:25 UTC and shows 10:25 in the app.
13. An activity from any source that starts at 23:30 Berlin and ends after midnight counts on the Berlin day it started.
14. The plan states, for Apple Health, which of the eight causes can occur, and each one that can is closed with its own test.
15. Every activity stored with a shifted time before the fix is corrected in place to its true start instant; the Movement usual uses corrected data from the day the correction runs; stored states of past days and cards already sent are unchanged (L2).
16. Each shifted activity is corrected exactly once; activities stored after the fix are untouched; running the correction a second time changes nothing.
17. With 50 Polar members connected, Calibra's Polar calls in a day stay within Polar's daily limit; the plan states the calls per member per day and the number of Polar members that fits (R1).
18. A Polar member whose last night is on Polar but not stored in Calibra opens the app and sees readiness for that night within 1 minute of opening, with no further action (R2). The calls this adds stay within R1.

## 6. Invariants

- Consent gating: nothing is stored for a member without the required consent.
- No wider read than the sources already allow and the 90 days of Q5; no new data type.
- Raw samples stay in the Open Wearables backend; only what is stored today is stored.
- User data stays in EU infrastructure.
- No rhythm, irregularity or condition analysis.

## 7. Non-goals

- Choosing between several HRV sources (parked).
- New sources.
- Data older than 90 days.
- Member-facing copy for missing nights (2.97.8 P18, P21).
- Readiness backfill rules (2.97.8).
- A "Connecting" state or new copy on the Polar row.
- Polar continuous heart rate and daily-activity start times stored one offset off (proposed as its own item, 2.96.8).
- Heart-rate zone minutes on workouts that were stored with a shifted time (BACKLOG).
- Showing activity times in Berlin time rather than device time in the app (BACKLOG).

## 8. Predicates

| # | Predicate | Value | Status |
|---|---|---|---|
| V6 | No loss | No night, activity or reading is lost because of something Calibra controls | agreed 13 Oct 2026 |
| Q5 | Window | Data is fetched as far back as the source serves it, up to 90 days after its date | agreed 13 Oct 2026 |
| N9 | Activity time | True start instant; offset subtracted; every activity, from every source, counts on the Berlin day it started | agreed 13 Oct 2026; every source agreed 14 Oct 2026 |
| L1 | Failed Polar registration, what the member sees | Settings > Connections shows Polar as "Not connected" with Connect until registration succeeds | agreed 13 Oct 2026 |
| R1 | Polar call budget | Polar calls stay within Polar's daily limit with 50 Polar members connected (per the CC report of 14 Oct 2026, the limit is 5000 plus 100 per user per day) | agreed 14 Oct 2026 |
| R2 | Night on app open | When a Polar member opens the app and last night is not stored, Calibra asks Polar then; readiness shows within 1 minute of opening if Polar has the night | agreed 14 Oct 2026 |
| L2 | Activities already stored with a shifted time | Corrected in place, each exactly once; the usual uses corrected data from the day the correction runs; past days' stored states and sent cards unchanged | agreed 13 Oct 2026 |

## 9. Data-protection ruling (calibra-dp-desk)

**Decided by Andrei, 13 Oct 2026, as R-2026-10-13-e** (queued for the next bundle).

- **Ruling:** covered, with conditions.
- **Why:** the same data types, read under the required consent, for the same purpose; fetching data the member's source already serves within 90 days is the existing ingestion (pattern of R-2026-10-06-a, R-2026-10-08-e); correcting start times is accuracy (Art. 5(1)(d)).
- **Conditions:** no new data type and no read older than 90 days; failure records hold codes, not free text, and follow the sync-status pruning (R-2026-10-06-a); nothing stored without the required consent; corrected times replace the wrong ones, each activity corrected once, no copy of the wrong time kept; nothing new reaches a model prompt; operational records the build adds (which activities hold a corrected time, the Polar registration state) hold identifiers and state only, no health values, and are deleted with the member on withdrawal and deletion (reading of R-2026-10-13-e, 14 Oct 2026).
- **Documents affected:** RoPA ingestion row only if it states a look-back shorter than 90 days; otherwise none.
- **DPO:** no need.
