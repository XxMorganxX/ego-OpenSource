---
type: person
role: Engineering lead
org: Orchard Labs
tags: [person, orchard-labs]
---
# Sam Rivera

Engineering lead on the [[Fruit-Inventory Service]] at Orchard Labs. Email: sam.rivera@example.com

## Work so far

- Wrote the original prototype in November 2025 ([[2025-11-02 prototype notes|Early Prototype Notes (retired)]]), retired once the [[2026-01-10 schema decision|schema decision]] landed.
- Drafted the SKU migration and shipped the unique-constraint fix: [[2026-01-12 pr review|PR Review: SKU migration]] and [[2026-01-22 dup sku bug|Bug: duplicate SKU on import]].
- Added the staleness alert after the [[2026-02-02 postmortem|cold-storage feed outage]].
- Proposed the append-only audit trail in [[2026-02-15 spec|Spec: SKU history audit trail]].
- Owned goals 1 and 3 on the [[2026-01-25 q1 roadmap|Q1 Roadmap]].
- Since March: building the data pipeline for [[Ripeness Forecast]].

## Notes

Likes small changes and a read-only dry run before any backfill. Pairs with [[Dana Whitfield]] on anything that needs staging. Wrote [[How to rerun the harvest import]] after the duplicate SKU incident.

#person
