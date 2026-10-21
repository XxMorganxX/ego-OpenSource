---
type: project
status: active
owner: Avery Quinn
tags: [project, fruit-inventory]
---
# Fruit-Inventory Service

The fruit-inventory service is Orchard Labs' system of record for every SKU, from harvest through cold storage to shipment. Owners: [[Avery Quinn]] (product) and [[Sam Rivera]] (engineering). Environments are run by [[Dana Whitfield]]'s platform team.

## Timeline

- 2025-11-02 — [[2025-11-02 prototype notes|Early prototype]] (retired)
- 2026-01-05 — [[2026-01-05 onboarding|Onboarding doc]]
- 2026-01-10 — [[2026-01-10 schema decision|Normalized schema chosen]]
- 2026-01-12 — [[2026-01-12 pr review|SKU migration reviewed]]
- 2026-01-17 — [[2026-01-17 weekly sync|Weekly sync]]
- 2026-01-20 — [[2026-01-20 vendor email|Cold-storage terms confirmed]]
- 2026-01-22 — [[2026-01-22 dup sku bug|Duplicate SKU bug]]
- 2026-01-25 — [[2026-01-25 q1 roadmap|Q1 Roadmap]]
- 2026-02-02 — [[2026-02-02 postmortem|Feed outage postmortem]]
- 2026-02-05 — [[2026-02-05 release notes|v1.4 released]]
- 2026-02-10 — [[2026-02-10 vendor renewal|Vendor renewal offer]]
- 2026-02-15 — [[2026-02-15 spec|Audit trail spec]]
- 2026-02-18 — [[2026-02-18 decision|Audit trail approved]]
- 2026-02-20 — [[2026-02-20 blog draft|Blog draft]]

## Tools

Alerts page on-call through Larchwire. Dashboards live in Pomewise. Imports run nightly; see [[How to rerun the harvest import]].

## Related

- [[Ripeness Forecast]] builds on the cold-storage feed.
- Decisions are collected in the [[Decision Log]].

#fruit-inventory
