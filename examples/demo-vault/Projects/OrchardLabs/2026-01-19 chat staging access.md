---
date: 2026-01-19
type: chat log
tags: [fruit-inventory, staging]
---
# Chat: staging access blocked

Copied from the team chat so the context does not get lost.

Sam: still waiting on staging DB access, can't test the migration.
Avery: pinged the platform team, should be unblocked by EOD.
Sam: thanks, will run the backfill as soon as it's up.
Avery: Dana says the staging copy needs a fresh snapshot first, so maybe tomorrow morning instead.
Sam: fine. I'll run the read-only dry run first either way.

Follow-up: [[Dana Whitfield]] provisioned staging on 2026-01-20. [[Sam Rivera]] ran the dry run the same afternoon and found 31 historical duplicate rows, all from two harvest batches loaded twice in December. See the [[2026-01-12 pr review|PR Review: SKU migration]] for how they were handled.

People: [[Avery Quinn]], [[Sam Rivera]], [[Dana Whitfield]]
