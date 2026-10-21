---
type: learning note
created: 2026-02-19
tags: [learning, data-modeling]
---
# Learning - Append-only audit trails

What I picked up while helping [[Sam Rivera]] think through the [[2026-02-15 spec|audit trail spec]]. Written in my own words, so treat it as notes rather than a reference.

## The core idea

Instead of changing a record in place, write down each change as a new row and never touch old rows. The current state is whatever you get by applying every change in order. Any past state is what you get by stopping early.

## Why it helps

- **Answering "what did we believe on Tuesday?"** An update-in-place table only knows the present. An append-only log knows every moment.
- **Explaining mistakes.** After the [[2026-01-22 dup sku bug|duplicate SKU bug]] we could see the bad rows but not the order they arrived in.
- **Trust.** Customers and auditors believe a number more when its history is visible.

## What it costs

- More rows. Usually fine, but worth measuring.
- Discipline. If one code path writes directly to the main table, the log silently stops being complete.
- Rebuilding state can be slow for long histories. A common trick is a periodic snapshot, so you replay from the latest snapshot instead of from the beginning.

## Things that are not changes

Sensor readings, such as the cold-storage temperatures, are observations. They are already append-only by nature and do not need a second log.

## Questions I still have

- How do you correct a wrong entry without editing it? Answer seems to be: append a correcting entry that points at the wrong one.
- How long should history be kept? We picked seven years in the [[2026-02-18 decision|decision record]].

#learning
