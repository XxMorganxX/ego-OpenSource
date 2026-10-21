---
date: 2026-02-02
type: postmortem
tags: [fruit-inventory, incident, cold-storage]
---
# Postmortem: cold-storage feed outage

The cold-storage vendor's API feed dropped for six hours on Feb 1. [[Avery Quinn]] escalated to the vendor; [[Sam Rivera]] added a staleness alert to the fruit-inventory service so a future gap pages on-call within 15 minutes instead of being caught manually.

## Timeline (Feb 1)

- 02:10 — last good reading from the Glacierline feed.
- 08:05 — a warehouse lead noticed the temperature panel in Pomewise had not moved since the night before.
- 08:20 — Avery called Noor Haddad at Glacierline Cold Storage; their side had an expired certificate.
- 08:25 — Sam confirmed no readings were lost on the vendor side, only delayed.
- 08:40 — feed restored and backfilled.

## What went wrong

Nothing watched the feed's freshness. We only found the gap because a person happened to look at a dashboard.

## What we changed

- Staleness alert: if no reading arrives for 15 minutes, page on-call. It went to the shared inbox at first; it now pages through Larchwire, see [[2026-03-11 Adopt Larchwire for paging]].
- Glacierline agreed to warn us before certificate rotations.

## Status

Resolved. The vendor relationship continues; Glacierline handled the escalation well.

## Lessons

A feed we depend on needs its own health check, not just the data it carries. Discussed again in the [[2026-03-31 Q1 Retro|Q1 Retro]].

#postmortem #incident
