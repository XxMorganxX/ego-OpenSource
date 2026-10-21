---
date: 2026-03-18
type: meeting notes
attendees: [Avery Quinn, Sam Rivera, Dana Whitfield]
tags: [meeting, ripeness-forecast, planning]
---
# Ripeness Forecast planning

Attendees: [[Avery Quinn]], [[Sam Rivera]], [[Dana Whitfield]]

Internal planning for [[Ripeness Forecast]] after the [[2026-03-04 Bramblecrest Grocers kickoff|kickoff]].

## Sam's first results

Sam ran the simple rule against last summer's batches. It predicted days to ripe within one day for 71% of pallets. The misses were mostly plums stored in the older cooler, which runs warmer than its setpoint. Sam wants to add the measured cooler temperature instead of the setpoint before trying anything more complicated.

## Platform needs

Dana will set up:

- A nightly job that computes the forecast for every pallet leaving cold storage.
- An alert in Larchwire if the job fails or produces no output, following the lesson from the [[2026-02-02 postmortem|feed outage]].
- A small table for the store spot-check results, typed in by Avery's team from Jules's photos.

## Decisions

- Keep the rule-based approach for the whole pilot. Revisit after August.
- Spot checks live in their own table, not in the audit trail; they are observations, not changes.

## Action items

- [ ] Sam: switch the rule to measured cooler temperature and rerun.
- [ ] Dana: nightly job and alert by April 15.
- [ ] Avery: confirm pilot store names with [[Jules Okafor]].

#meeting #planning
