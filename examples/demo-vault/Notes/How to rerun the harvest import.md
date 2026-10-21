---
type: how-to
created: 2026-01-27
author: Sam Rivera
tags: [how-to, import, fruit-inventory]
---
# How to rerun the harvest import

Use this when the nightly harvest import fails or stops partway, for example after a network timeout. Written by [[Sam Rivera]] after the [[2026-01-22 dup sku bug|duplicate SKU bug]].

## Before you start

- You need write access to production, which [[Dana Whitfield]] grants per incident.
- Check the Pomewise "Imports" panel. Note the batch id and how many rows were loaded before the failure.
- If a Larchwire page is open for the import, acknowledge it so nobody else starts the same rerun.

## Steps

1. **Dry run first.** Run the import in read-only mode against the same file. It prints how many rows would be inserted and how many would be rejected as duplicates.
2. **Read the numbers.** Rows already loaded should show up as rejected duplicates. If the dry run says it would insert rows that are already in Pomewise, stop and ask Sam.
3. **Run it for real.** Same file, write mode. Since the unique constraint from the [[2026-01-12 pr review|SKU migration]] went live, a repeated batch and SKU pair is refused instead of counted twice, so a rerun is safe.
4. **Check the totals.** The batch total in Pomewise should match the harvest sheet from the orchard.
5. **Write it down.** Add a line to the import log with the time, the batch and what happened.

## Do not

- Do not delete rows by hand to "make room" for a rerun.
- Do not rerun a batch that is older than seven days without asking [[Avery Quinn]]; customers may already have been invoiced.

#how-to
