---
date: 2026-03-11
type: decision record
status: accepted
owner: Dana Whitfield
tags: [decision, platform, on-call, larchwire]
---
# Adopt Larchwire for paging

Decision: the platform team will use Larchwire for all on-call paging at Orchard Labs, starting April 1. Owner: [[Dana Whitfield]].

## Context

After the [[2026-02-02 postmortem|cold-storage feed outage]], [[Sam Rivera]] added a staleness alert, but it could only send email to a shared inbox. Nobody reads that inbox at two in the morning. We needed a tool that actually wakes someone up and knows whose turn it is.

## Options considered

- **Keep the shared inbox.** Free, and proven not to work.
- **Larchwire.** Rotations, quiet hours, escalation after ten minutes, and a simple web hook for our own alerts.
- **Brightpost Pager.** Similar features, but every alert needs a fixed template and it has no quiet hours.

## Why Larchwire

Quiet hours matter for a team of three: low-priority alerts wait until 07:00, and only the feed staleness alert and import failures page at night. Escalation means a missed page reaches [[Avery Quinn]] after ten minutes.

## Consequences

- Sam and Dana share the rotation week by week; Avery is the escalation.
- The [[Ripeness Forecast]] nightly job gets a Larchwire alert from day one.
- Added to the [[Decision Log]].

#decision #on-call
