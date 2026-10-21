# 2.96.8 Polar continuous heart rate at its true time: Spec v1.1

Status: v1.1, 21 Oct 2026. Becomes the contract on Andrei's agreement to the five build decisions of 21 Oct; until then v1.0 stands.
Change from v1.0 (after CC's pre-build read, relayed 21 Oct 2026): Polar sleep records store no offset, so P1's offset is read from Polar's own sleep data at ingest (option A); P1 wording corrected (re-ratification on Andrei's agreement); new P7 call budget; AC9 rewritten (clean-up method); two limits added to non-goals; DP ruling note corrected.
Earlier changes: v1.0 ratified P1 to P6 and decided R-2026-10-21-c; v0.2 dropped the pre-build verification query (AC10 is the check).
Product item: 2.96.8 Polar heart-rate and daily-activity times (Notion 3f98f5efacd281d78968d6f6f8aea2fe). Build child: 2.96.8.1 (Notion 4008f5efacd281d49260ef05cec0cd09). Parent: 2.96 Wearable integrations at launch.
Release: 30 Oct 2026, as an exception to the release scope freeze (Andrei, 21 Oct 2026, option A).
Repo: OW fork.

Technical facts below are attributed to CC's read-only investigation and pre-build read, both relayed by Andrei on 21 Oct 2026 ("relay 21 Oct"), against OW fork `release/0.9.0-syn` at 815a978b. They are inputs to this spec, not verified system behaviour.

## 1. Intent

- Who: every member connected to Polar whose UTC offset is not 0 (most of the 40 launch countries: +1 in winter, +2 for Finland, Greece, Romania, more in summer).
- Pain: per relay 21 Oct, Polar continuous heart rate is stored one UTC offset later than it happened. Overnight heart rate then mixes awake pre-sleep heart rate into the night, and workout heart-rate zones read the hour before the session.
- What the member sees wrong: "Lowest heart rate overnight" and "Average heart rate overnight" (2.102.1), Core Readiness (overnight heart rate is a Core input), workout zones and the workout load that feeds the Movement usual, and chat, which carries the overnight values (R-2026-10-15-a).
- Why now: baselines and usuals form from a member's first nights. Fixed before the production wipe, no production row needs correcting. Fixed after launch, live members' baselines shift and, per relay 21 Oct, stored continuous rows cannot be told apart from the sleep record's own heart-rate rows, so there is no clean correction path.

## 2. Affected surfaces

- OW fork: `normalize_continuous_hr` and `_hhmm_to_datetimes` in `backend/app/services/providers/polar/data_247.py` (per relay 21 Oct: anchor built from Polar's bare date at :464, zone copied onto samples at :201).
- OW fork: both ingest paths for Polar continuous heart rate, the full pull and the webhook.
- OW fork: Polar's sleep listing `/sleep/available`, already called on every pull (per relay 21 Oct, it lists 28 nights with their offsets), read at ingest as an offset source.
- OW fork: `zone_offset` on stored Polar workouts (per relay 21 Oct, `workouts.py:140`), read as an offset source.
- OW fork: `data_point_series` rows with source `polar` and series type heart rate.
- OW fork: the per-member Polar call budget in `pull_budget.py`.
- OW fork: FORK-DELTA entry for the change.
- Dev only: the internal Polar account's stored Polar heart-rate rows.
- Adapter, read only, no code change expected (per relay 21 Oct): `NightHeartRate.cs`, OW zone calculation and `WorkoutHrZoneHealService.cs`, the `rhr-coverage` recompute with `?days=28` for the dev check.

## 3. Acceptance criteria

- AC1: A Polar continuous heart-rate sample recorded at local wall time T, on a date where the member's UTC offset is O, is stored at instant T minus O (P1). Tested with fixtures at O = 0, +1, +2 and +3, on both the full pull and the webhook path.
- AC2: For a fixture night with sleep start S and end E (stored as true instants), every continuous sample stored inside [S, E] was recorded between S and E local time. No sample recorded before S is stored inside [S, E].
- AC3: For a fixture workout from 18:00 to 19:00 local at O = +1, the heart-rate series read over the workout window contains the samples recorded 18:00 to 19:00 local, and none recorded 17:00 to 18:00.
- AC4: Polar daily-activity stored times are identical before and after the change for the same payload.
- AC5: Polar sleep record start, end, its own heart-rate samples and hypnogram are identical before and after the change for the same payload.
- AC6: Polar workout start and end times (fixed in 2.96.6.1) are identical before and after the change for the same payload.
- AC7: Ingesting the same continuous heart-rate payload twice after the change, by any mix of full pull and webhook, leaves the stored sample count unchanged (P2).
- AC8: No continuous heart-rate sample in a fixture payload is dropped: stored count after the change equals the payload's sample count (P3 covers days with no offset source).
- AC9: After the change is deployed on dev, the internal Polar account's Polar heart-rate rows for the last 28 days are at their true instants with no duplicates, and an `rhr-coverage` recompute with `?days=28` has run. Method, in order of preference: (a) disconnect and reconnect Polar on that account, if that removes its Polar rows and re-pulls 28 days; (b) otherwise an idempotent startup data step that does nothing unless a dev-only deployment variable names the account, runs once, and never touches sleep records. No Alembic migration carrying an account id. No manual database step.
- AC10: Polar account check (P4), which is also the check that Polar's continuous times were local: for 3 nights, the time of the lowest heart rate overnight shown in Calibra is within the tolerance in P4 of the low in Polar Flow's 24/7 heart-rate graph, and for 1 workout the zone minutes shown in Calibra are within the tolerance in P4 of Polar Flow's zone minutes. Andrei runs the side-by-side. If AC10 fails, the change does not ship in the 30 Oct release.
- AC11: The PR states the worst-case Polar calls per member per day with the change, and it is within P7.

## 4. Invariants

- Signal independence: no change to how Readiness, Context, Movement or Nutrition are computed or combined.
- EU residency: data stays in the existing EU stores; no new store, processor or transfer.
- Consent gating unchanged; deletion and export of these rows unchanged (raw samples remain in the Art. 20 part of the export).
- No-diagnosis line: no new member-facing copy.
- No lost wearable data (2.96.6): the change never discards a Polar sample.
- Nothing new is stored: no offset or time zone is written on heart-rate rows or sleep records; `zone_offset` on heart-rate rows stays empty.
- The night rule (2.96.3: a night is sleep starting 18:00 to 06:00 local) and the 2.96.9 day rules are unchanged.
- Production rows are not corrected by this item; the decided production data wipe before the first founding-cohort member covers them.

## 5. Non-goals

- Polar daily-activity start times. Per relay 21 Oct they land on the member's correct day today, and storing their true instant would move days for members east of Berlin's offset.
- The clocks-back night (per relay 21 Oct, a backward step in Polar times may be read as passing midnight, filing later samples a day late). Next occurrence 31 Oct 2027. Separate item.
- Polar skin temperature for Advanced Readiness (2.96.10).
- Polar body (wrist) temperature timestamps.
- Storing an offset on Polar sleep records or heart-rate rows (options B and C of 21 Oct, rejected).
- Open Wearables' own daily heart-rate summaries, which feed only the export: per relay 21 Oct, samples just after local midnight may land on the UTC date. Accepted limit.
- P3 duplicate: a sample stored uncorrected under P3 is stored again at its true time if Polar later serves a sleep record for that date, and the uncorrected row stays. Accepted limit (needs no Polar sleep or workout within 3 days either side).
- Any adapter read-code change.
- Re-deriving zones for stored workouts beyond what the dev check needs.
- Any member-facing copy change.

## 6. Predicates

- P1 True instant: stored instant = local wall time minus the UTC offset in force for the member at that moment, read at ingest from Polar's sleep data for that night (sleep listing) or from that day's stored Polar workout `zone_offset`. `ratified 21 Oct 2026; wording corrected in v1.1, re-ratified on Andrei's agreement`
- P2 Re-ingest: ingesting the same payload twice adds 0 rows. `ratified 21 Oct 2026`
- P3 No offset source: a continuous sample on a date with no Polar sleep record and no Polar workout is still stored, using the member's nearest Polar sleep or workout offset within 3 days before or after; if none exists, stored as today (no correction) and counted in the build's report. Such samples feed no member-facing read at launch. `ratified 21 Oct 2026`
- P4 Polar account check tolerance: time of the overnight low within 10 minutes of Polar Flow; workout zone minutes within 2 minutes per zone of Polar Flow. `ratified 21 Oct 2026`
- P5 Clocks-change nights: an error of up to 1 hour for samples after the change on the two nights a year is accepted for this item. `ratified 21 Oct 2026`
- P6 Cut-off: merged by end of 28 Oct 2026, Polar account check done by end of 29 Oct 2026; if missed, the item moves to the first release after launch. `ratified 21 Oct 2026`
- P7 Call budget: worst-case Polar calls per member per day, including any extra call on the webhook path, stay at or under 200. `ratified on Andrei's agreement`

## 7. Data-protection ruling

- R-2026-10-21-c (decided by Andrei, 21 Oct 2026): covered by the existing ingestion processing, with conditions.
- Why: same data, purpose, basis and store; storing the true instant improves accuracy (Art. 5(1)(d)).
- Conditions: no new stored field holding a member's UTC offset or time zone beyond what is already stored (per relay 21 Oct, only Polar workouts store an offset; Polar sleep records do not); dev clean-up and re-pull touch only the internal staff Polar account; no production rows touched by this item.
- v1.1 note: option A reads Polar's offsets at ingest and stores nothing new, so the conditions hold as decided. Options B and C would each store a new offset and would go back to the desk.
- Documents affected: none.
- DPO: no need.
