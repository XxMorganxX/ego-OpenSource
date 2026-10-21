---
date: 2026-02-05
type: release notes
version: "1.4"
tags: [fruit-inventory, release]
---
# Release Notes v1.4

v1.4 of the fruit-inventory service: normalized SKU schema live in production, cold-storage vendor feed integrated, duplicate-SKU bug fixed. Thanks to [[Sam Rivera]] and [[Avery Quinn]] for driving this.

## Shipped in this release

- **Normalized schema.** Harvest batches, SKUs, pallets and locations now live in separate tables, as planned in the [[2026-01-10 schema decision|Fruit DB Schema Decision]]. The old flat table is read-only.
- **Cold-storage feed.** Temperature and humidity readings from Glacierline Cold Storage arrive every 15 minutes and show next to each pallet.
- **Duplicate-SKU fix.** The import rejects a repeated batch and SKU pair; see [[2026-01-22 dup sku bug|Bug: duplicate SKU on import]].
- **Staleness alert.** A gap in the feed pages on-call within 15 minutes, added after the [[2026-02-02 postmortem|feed outage]].

## Known issues

- Pallets received before January still show "unknown batch" until the warehouse team confirms them.
- The temperature chart in Pomewise loads slowly for pallets older than 60 days.

## Upgrade notes

No action needed for warehouse staff. Report anything odd to Sam.

This release covers Q1 goals 1 to 3 on the [[2026-01-25 q1 roadmap|Q1 Roadmap]].

#release
