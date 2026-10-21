---
date: 2026-02-20
type: blog post draft
status: draft
tags: [fruit-inventory, blog]
---
# Blog Draft: how Orchard Labs tracks fruit

Draft for the Orchard Labs engineering blog. Covers why we normalized the fruit-inventory schema, what we learned from the cold-storage outage, and how [[Avery Quinn]] and [[Sam Rivera]]'s team built an append-only audit trail.

## Working outline

1. **One table was enough, until it was not.** The [[2025-11-02 prototype notes|first prototype]] stored one row per pallet. That worked until pallets started being split at packing and nobody could say which harvest batch a crate came from.
2. **Choosing a schema.** Three options, one winner. Short version of the [[2026-01-10 schema decision|schema decision]].
3. **The night we counted plums twice.** The [[2026-01-22 dup sku bug|duplicate SKU bug]] and the one-line constraint that fixed it.
4. **A feed is a dependency.** What the [[2026-02-02 postmortem|six-hour feed outage]] taught us about watching freshness, not just data.
5. **Remembering every change.** Why we approved the [[2026-02-18 decision|append-only audit trail]].

## Notes for the editor

- Keep it under 1,500 words.
- No customer names. Say "a regional grocery chain" instead of Bramblecrest Grocers.
- Ask Sam for one diagram of the tables.

#blog
