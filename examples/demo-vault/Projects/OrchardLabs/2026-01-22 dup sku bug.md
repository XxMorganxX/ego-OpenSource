---
date: 2026-01-22
type: bug report
severity: blocker
tags: [fruit-inventory, bug, import]
---
# Bug: duplicate SKU on import

Orchard Labs' import job double-counted 12 SKUs after last night's harvest batch upload. [[Sam Rivera]] traced it to a missing unique constraint on (batch_id, sku). Filing as a blocker for the schema migration in the design doc ([[2026-01-10 schema decision|Fruit DB Schema Decision]]).

## What happened

The nightly import retried after a network timeout halfway through the upload. The retry started from the top of the file instead of where it stopped, so the first 12 SKUs of the batch were loaded twice. Nothing in the old tables refused the second copy, and the morning stock report showed 12 crates of plums that did not exist.

## Impact

- Stock counts for one harvest batch were overstated for about nine hours.
- Two customer orders were promised against stock we did not have. [[Avery Quinn]] called both customers before noon.

## Resolution (2026-01-24)

The fix: the unique constraint from the [[2026-01-12 pr review|PR Review: SKU migration]] was merged and deployed once staging was available. A repeated (batch_id, sku) pair is now rejected, and the import logs the rejected row instead of counting it again. The 12 extra rows were removed by the backfill. Zero duplicate-SKU incidents is goal 3 on the [[2026-01-25 q1 roadmap|Q1 Roadmap]].

#bug #import
