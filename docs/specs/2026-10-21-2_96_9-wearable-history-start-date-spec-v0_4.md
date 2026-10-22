# 2.96.9 Wearable history starts when the member started wearing

Spec v0.4, Product Studio, 21 Oct 2026 (v0.1 to v0.3 the same day). Roadmap item 2.96.9 (parent 2.96 Wearable integrations at launch), Notion `4008f5efacd28188b0dce9b6bd15289e`, Stage Approved. Build child 2.96.9.1.
Status: APPROVED, ready for build. All predicates ratified by Andrei on 21 Oct 2026. Data-protection rulings R-2026-10-21-a and R-2026-10-21-b decided by Andrei on 21 Oct 2026. Ships in the 30 Oct 2026 release, an exception to the release scope freeze (Andrei, 21 Oct 2026); the S12 deletion may follow, but must be live before any production source reaches its freeze (section 10).
Source: Andrei's own account (Polar Loop Gen 2, account created 13 Oct 2026; he started wearing the device on 13 Oct 2026), his member export of 21 Oct 2026, the CC read-only diagnosis reports relayed 21 Oct 2026, and the build plan from the GPT-6.1 Sol session relayed 21 Oct 2026 (code read at adapter `ebcbf036` and Open Wearables `815a978b`, nothing run).

Changes from v0.3 (Andrei, 21 Oct 2026, on the Sol build plan):
- S4 changed: a source's start date is provisional and recomputed as its nights arrive, then frozen. This removes the need to know when Apple's first history upload is complete.
- S11 added: before the freeze, readings, baselines and prompts use only data on or after the provisional date.
- S12 added: at the freeze, history before the start date is deleted from both stores; pre-start data arriving afterwards is not stored. History is therefore held temporarily, not refused at the door (R-2026-10-21-b).
- R1 to R5 ratified (spec readings raised by the plan): trailing gap, first-night cutoff, weight, export part, nutrition results.
- Section 10: release sequence.

Changes from v0.2 (Andrei, 21 Oct 2026): S8 reading confirmed; S10 ratified; R-2026-10-21-a decided; release target 30 Oct 2026.

Changes from v0.1 (Andrei, 21 Oct 2026): S6 ratified as changed (night and (workout or steps)); S7 ratified; S8 ratified as changed (start date per source); S9 withdrawn (settled by the night rule, 2.96.3 P6); S10 added.

Amends, where they differ: 2.97.1 U8 (history at connect), U22 and AC 26 (day with data, Movement); 2.96.6 Q5 (window) for dates before a source's start date; 2.97.8 P8 and AC 14 ("past nights available at connect count"); 2.97.4.4 V4, AC 12 and AC 13 (history at connect).

## 1. Intent

**Who.** Every member who connects a wearable or Apple Health with history in it. Most acute for a device that was shared, second-hand, in a drawer, or worn by someone else before the member connected it.

**Pain.** Calibra imports history at connect and treats every stored row as the member's own. Per the export of 21 Oct 2026, Andrei's account imported 26 Sep to 12 Oct, before he wore the watch: 17 days of step rows, most at 0 steps, 21 short sessions recorded by another wearer, and one night (27 Sep). His Movement usual came out at 0.34 to 1.08 load units against 2.3 to 9.2 actually spent per day from 13 Oct. Result: Movement read Rest every day from 13 to 20 Oct, and on 20 and 21 Oct the day read Stretched with "Rest today" (rule `m_week_well_above_usual_protected`) while Readiness read Good and the calendar was empty. The 27 Sep night also entered his sleep-duration and overnight heart-rate baselines from day 1.

**Why now.** Every founding member reaches Day 8 with whatever history was imported. A member who trains regularly and whose device history is thin, gappy or someone else's gets told to rest in their first real week, and Polar or Garmin shows them the opposite. That is the comparison Calibra loses.

**What this slice delivers.** Each connected source gets a data start date, worked out from that source's own nights during its first week and then frozen. Readings use only data from that date; older history is deleted at the freeze. A day the member did not wear their device through the night and day is no data, not a rest day.

## 2. Definitions

- **Recorded night:** as the night rule defines it (2.96.3 P6): sleep starting between 18:00 and 06:00 local; naps never count. Which day a night belongs to is also as the night rule settles it.
- **Source:** one wearable connection by provider (Polar, Apple Health). Apps writing into Apple Health stay inside the Apple Health source.
- **Connection day:** the Berlin day the source was first connected for this account. A reconnection does not change it.
- **History window:** the 28 calendar days before the connection day, plus the connection day itself.
- **Gap:** 3 or more consecutive calendar days with no recorded night from that source (S2).
- **Freeze:** the end of the 7th Berlin day of the source's connection, counting the connection day as day 1. A source connected on the sign-up day freezes at the end of Day 7, before a coordination state can exist (2.97.4.4).
- **Source start date:** per source; provisional until the freeze, then fixed (S3, S4, S8).
- **Day with data, Movement:** S6, or S10 for a member whose sources have recorded no night.
- **Wearable data:** sleep, nights (HRV, overnight heart rate, skin temperature), heart-rate and step samples, daily activity totals, workouts. Not wearable data for this spec: calendar, nutrition, and weight (R3).

## 3. Behaviour

### 3.1 Working out a source's start date (S1 to S4, S7, S8, R1)

- From the moment a source connects, Calibra recomputes its start date each time nights from it arrive, until the freeze.
- The start date is the day of the first recorded night after the latest gap in the history window. With no gap, it is the day of the first recorded night in the window.
- When the window holds no recorded night, or ends in a gap with no recorded night after it (R1), the start date is the connection day (S7).
- Nights only ever add evidence: a later batch can fill an apparent gap, which moves the date earlier. Arriving nights never move it later.
- At the freeze the date is fixed. After that, days without a night, a holiday without the device, a disconnection or a reconnection never move it (S4).
- A source connected later works out its own date by the same rules, from its own nights and connection day (S8).

### 3.2 Before the freeze (S11)

- History from before the provisional start date is held only to work out the date.
- Every reading, baseline, warm-up count, card and prompt uses only data on or after the source's current provisional date. When the date moves, readings follow it.
- The first night counts whole, with its in-sleep samples, under the night rule (R2).

### 3.3 At and after the freeze (S12, S5)

- At the freeze, everything from that source dated before its start date is deleted from Calibra's database and from the Open Wearables backend, including temporary upload files and archives. A failed deletion is retried until it succeeds and is never reported as done.
- Data dated before a source's start date that arrives after its freeze is not stored.
- From the start date on, everything from that source is kept as today, including a workout on a day without a recorded night (S5). The 2.96.6 late-data rules apply from the start date on.

### 3.4 Days with data (S6, S10)

- A day counts toward the Movement usual when it has a recorded night and either a workout or more than 0 steps. The night and the movement may come from different sources.
- Any other day is no data: it does not enter the Movement usual (2.97.1 U5). This includes a day with a night but no steps and no workout, which 2.97.1 U22 counted as 0.
- A workout on a day with no recorded night counts in that day's Movement load and in the Movement week (2.97.2 V7), but the day does not enter the Movement usual. The usual and the week's actual load are therefore two separate inputs.
- S10: for a member none of whose sources has recorded a night, a day counts with a workout or more than 0 steps. From the first recorded night on, S6 applies.

### 3.5 Baselines and warm-up

- Every wearable baseline and warm-up count uses only data on or after the start date of the source it came from: 2.97.8 P8 qualifying nights, the HRV, sleep and overnight heart-rate baselines (`signal_baselines`), and the 2.97.1 Movement usual.
- No other rule of those specs changes: window lengths, the block weighting (2.97.2 V10), the 7-day minimum, the night rule and the 2.96.6 retry rules stand.

### 3.6 What the member sees

- Nothing new in the app. No start date is shown or reaches a prompt.
- The export carries each source's start date in its computed-values part, Part 2 (R4). Before a freeze, the export holds what is stored at that moment.
- Calendar and nutrition records and rules are unchanged. Nutrition results can still shift because their wearable inputs change (R5).

## 4. Affected surfaces

From the Sol build plan relayed 21 Oct 2026 (adapter `ebcbf036`, Open Wearables `815a978b`). Sol confirms each in the build.

- Open Wearables: historical sync on connect (`oauth.py:113`, requests 90 days per the plan); Apple import (`sdk_sync.py:115`, `process_sdk_upload_task.py:396`, `import_service.py:418`); deletion at the freeze across samples, events and details, archives (`data_point_series_archive.py`), health scores (`health_score.py`), temporary payloads (`raw_payload_storage.py`, which per the plan swallows deletion failures today) and sleep state; source identity (`data_source.py:36`); start date storage per the plan (`user_connection`).
- Adapter, Movement: `UsualHistory.cs:37` (usual eligibility, S6 and S10); `SclReaders.cs:165` and `CoordinationInputs.cs:176` (separate actual-load input for the week); step history `StepsSweepOrchestrator.cs:182`, `OpenWearablesClient.cs:230`, `StepDayWriter.cs:44` (filter per source before dedup).
- Adapter, nights and baselines: `NightIngestService.cs:124` and `:160`, `NightAssembler.cs:77`, `NightClock.cs:46`, `RhrCoverageSweepOrchestrator.cs:164`, `PgNightHeartRateStore.cs:41`, `ReadinessReaders.cs:32`, `RunReadinessForUserService.cs:134` and `:286`, `PgOwPhysiologySeriesWriter.cs:52`.
- Adapter, first week: `FirstWeekReader.cs:88`, `MovementWarmUpReader.cs:39`.
- Adapter, connection and start dates: `WearableConnectEndpoints.cs:293`, `WearableConnectionRecord.cs:72` and `:89` (reconnect advances the time and disconnect deletes the row, so start dates need their own store per the plan), `OwEndpoints.cs:156` (Apple connection at token mint); erasure in `AccountDeletionRepository.cs:323`.
- Export: `ExportArchiveBuilder.cs:62`, `ExportCatalog.Columns.cs:305`, `ExportStager.cs:63`.
- Not affected: calendar ingestion and its usual reader (`UsualHistory.cs:77`); nutrition; weight (R3); the app.

## 5. Acceptance criteria

1. A source whose history window holds a recorded night on day -24 and every night from day -8 to day -1 (connection on day 0, no night on days -23 to -9) has the start date of day -8 at its freeze.
2. A source whose history window holds a recorded night every night with no gap has the start date of the first night in the window, also when its nights arrive across several batches.
3. A source whose provisional date is day -8 and which then receives a batch with nights on days -23 to -9 has the start date of day -24 or earlier at its freeze; no record from those days was deleted before the freeze.
4. The provisional start date never moves later when nights arrive.
5. At the freeze, no record from that source dated before its start date remains in `movement_daily`, `training_activities`, `physiology_daily`, `sleep_daily`, `readiness_daily`, `signal_baselines` or any Open Wearables store, temporary upload files and archives included; records on or after it, other sources' records and other members' records are untouched.
6. A deletion that fails at the freeze is retried and the source is not marked as cleaned until every store confirms it.
7. After the freeze, data from that source dated before its start date, arriving by any path (pull, webhook, upload retry, sweep), is not stored.
8. Before the freeze, no reading, baseline, warm-up count, card or prompt uses data from before the source's current provisional date.
9. A source whose window has no recorded night, or ends in a gap of 3 or more days with no night after it, has its connection day as the start date.
10. A source connected on the sign-up day freezes at the end of Day 7.
11. Disconnecting and reconnecting a source keeps its connection day and, after the freeze, its start date.
12. A second source connected later has its own start date from its own nights and connection day; its records on or after that date are kept even when dated before the first source's start date.
13. Andrei's fixture: with history equal to his export of 21 Oct 2026 (a night on 27 Sep; nights every night from 13 Oct; 21 sessions from 27 Sep to 11 Oct; step rows from 26 Sep with 0 steps on most days to 12 Oct; session loads and step totals synthetic where the export gives none) and the daily Movement loads of 13 to 20 Oct from the same export (4.5, 5.1, 9.21, 2.33, 2.36, 8.98, 3.58, 5.91), the Polar start date is 13 Oct; the Movement usual for 20 Oct is built only from 13 to 19 Oct (about 5.15 load units a day); the Movement week ratio on 20 Oct (2.97.2 V7, V8, Readiness Good) is below 1.3 (about 1.04); and neither `m_week_well_above_usual_protected` nor the Loaded Movement rung fires on 20 Oct.
14. A day with a recorded night and no steps and no workout does not enter the Movement usual, including when a physiology row exists for it.
15. A day with a recorded night from one source and more than 0 steps from another enters the Movement usual.
16. A workout on a day with no recorded night counts in that day's Movement load and in the Movement week, and the day does not enter the Movement usual.
17. A member whose sources have never recorded a night, with 7 or more days carrying a workout or more than 0 steps, has a Movement usual; from the first recorded night, S6 applies.
18. The 2.97.8 P8 warm-up count, the HRV, sleep and overnight heart-rate baselines and the Movement usual include no data from before the start date of its source.
19. Deleting and re-creating an account works out new start dates from the new connections (pattern of 2.97.4.4 H4).
20. The member export carries each source's start date in Part 2 only; after the freeze it holds no wearable record from before its source's start date.
21. No API response used by the app, no screen and no prompt carries a start date.
22. Calendar, nutrition and weight records are stored and read exactly as before, whatever the start dates.

## 6. Invariants

- Signal independence; no single score; a coordination state is never a score.
- No new data type and no wider read than today (2.97.1 U8, 2.96.6 Q5).
- No imputation: a day before a start date is never filled in from another source.
- The night rule (2.96.3 P6) and one HRV source per member (2.96.3) stand.
- Within-person baselines; the usual is never shown as a number.
- Consent gating; user data stays in EU infrastructure.
- No-diagnosis line; nothing infers who wore the device.

## 7. Non-goals

- Asking the member when they started wearing the device (rejected by Andrei, 21 Oct 2026).
- Knowing when Apple's history upload is complete; a batch that arrives after the freeze with nights from before the start date is not kept (accepted limit).
- Detecting a different wearer inside the history when there is no gap, or after a start date.
- Rewriting existing accounts: the production wipe this week clears every account before go-live (Andrei, 21 Oct 2026).
- New app copy or a new app build.
- Calendar backfill (2.97.5); the "my normal has changed" reset and the Away marker (2.97.4).
- Any change to the usual's arithmetic, warm-up lengths, bands or the coordination rules.
- The frozen morning briefing, the HRV rounding difference and the other defects found in the same diagnosis (tracked separately).

## 8. Predicates

| # | Predicate | Value | Status |
|---|---|---|---|
| S1 | Physiology day | A recorded night, per the night rule (2.96.3 P6) | ratified 21 Oct 2026 |
| S2 | Gap | 3 or more consecutive calendar days with no recorded night | ratified 21 Oct 2026 |
| S3 | Start date | Day of the first recorded night after the latest S2 gap inside the 28-day history window; with no gap, the first recorded night in the window | ratified 21 Oct 2026 |
| S4 | When it is fixed | Provisional and recomputed as the source's nights arrive; frozen at the end of the 7th day of the source's connection (connection day = day 1); never moves after the freeze | ratified 21 Oct 2026 (changed from "set once at first import") |
| S5 | Training and steps without a night | Dropped only in history before the start date; every session on or after it counts | ratified 21 Oct 2026 |
| S6 | Day with data, Movement | A recorded night and (a workout or more than 0 steps); otherwise no data. Amends 2.97.1 U22 and AC 26 | ratified 21 Oct 2026 |
| S7 | No recorded night in a source's window | Start date = connection day | ratified 21 Oct 2026 |
| S8 | Start date per source | Each source by S1 to S4 from its own nights and connection day; its days follow S6 | ratified 21 Oct 2026 |
| S9 | Night date | Withdrawn: settled by the night rule (2.96.3 P6) | withdrawn 21 Oct 2026 |
| S10 | Member whose sources record no nights | A day counts with a workout or more than 0 steps; S6 from the first recorded night on | ratified 21 Oct 2026 |
| S11 | Before the freeze | Readings, baselines, warm-up counts, cards and prompts use only data on or after the current provisional date; older history held only to work out the date | ratified 21 Oct 2026 |
| S12 | At and after the freeze | Everything from the source dated before its start date deleted from both stores, temporary files included; later pre-start data not stored | ratified 21 Oct 2026 |
| R1 | Window ending in a gap | Treated as S7: start date = connection day | ratified 21 Oct 2026 |
| R2 | First night | Counts whole, with its in-sleep samples, by the night rule. Exception: if the first night reaches Open Wearables only after the freeze, its samples checked before it is stored and dated (Berlin day) before the start date are not kept (no retry; one reading could otherwise block a member's sync) | ratified 21 Oct 2026; exception 22 Oct 2026 (Dragan) |
| R3 | Weight | Outside the cutoff, as today | ratified 21 Oct 2026 |
| R4 | Export | Start dates in Part 2 (computed values) | ratified 21 Oct 2026 |
| R5 | Nutrition | Records and rules unchanged; results may shift through wearable inputs | ratified 21 Oct 2026 |

The freeze moment (end of the 7th day, connection day = day 1) is Product Studio's reading of the ratified S4 wording "the end of the 7th day after that source connected", chosen so a sign-up-day source freezes before Day 8. Andrei can change it.

## 9. Data-protection rulings (calibra-dp-desk, 21 Oct 2026)

**R-2026-10-21-a, decided by Andrei, 21 Oct 2026**, amended by **R-2026-10-21-b, decided by Andrei, 21 Oct 2026.**

- **Ruling:** covered, with conditions. Narrows processing compared with today.
- **Why:** same data types, purpose and required consent; much less is kept. Minimisation and accuracy (Art. 5(1)(c), (d)). Narrows R-2026-10-08-e and R-2026-10-13-e. R-2026-10-21-b replaces "not imported" with "held until the freeze, then deleted", so the date can be worked out without knowing when an upload is complete. The existing privacy text already covers the history read (Andrei, 21 Oct 2026).
- **Conditions:**
  - History from before a source's start date is held at most until that source's freeze, used only to work out the date, and never in a reading, baseline, card or prompt (AC 8).
  - At the freeze it is deleted from the Calibra database and the Open Wearables backend, temporary upload files and archives included; failures are retried, never reported as done (AC 5, AC 6).
  - Pre-start data arriving after the freeze is not stored (AC 7).
  - Start dates are deleted with the member on withdrawal and deletion, are in the export's Part 2, and are never in a prompt or on a screen (AC 20, AC 21).
  - No text claims Calibra holds only the member's own data.
  - The deletion is live before any production source reaches its freeze.
- **Documents affected:** RoPA row "Health-data ingestion & guidance (core)" (history held until the first week's freeze, then deleted); DPIA §4.3 at the next revision; the change note section rewritten.
- **DPO:** a line in the running change note.

## 10. Release

- 30 Oct 2026 release (exception to the freeze, Andrei, 21 Oct 2026): start dates worked out and stored, S11 reader filtering, S6 and S10, the separate actual-load input, rejection of pre-start data after the freeze.
- The S12 deletion may ship after 30 Oct, but must be live before any production source reaches its freeze, that is before the end of the first founding member's 7th day after connecting.
- The production wipe this week clears every account before go-live, so no clean-up of existing accounts is needed.
