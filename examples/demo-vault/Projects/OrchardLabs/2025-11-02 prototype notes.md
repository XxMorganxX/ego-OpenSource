---
date: 2025-11-02
type: prototype notes
status: retired
tags: [fruit-inventory, prototype, retired]
---
# Early Prototype Notes (retired)

> Retired: superseded by the [[2026-01-10 schema decision|Fruit DB Schema Decision]]. Kept for history.

Author: [[Sam Rivera]]

First pass at a fruit-inventory prototype for Orchard Labs, written before we had chosen a schema. It was a single flat table, one row per pallet, with the SKU, harvest date, orchard block and a free-text location column. Simple to build in a weekend and good enough to demo to [[Avery Quinn]].

What it taught us:

- One row per pallet hides the harvest batch. When a pallet is split at packing, we lose the link back to the batch it came from.
- Free-text locations ("cooler 2", "Cooler #2", "c2") made every report wrong in a slightly different way.
- Nothing stopped the same SKU from being loaded twice. We noticed duplicates by eye and deleted them by hand.

The early cold-storage idea was to skip a vendor entirely and bolt our own temperature sensors onto the two rented coolers, logging into the same flat table. We priced it, found the sensors drifted badly below four degrees, and abandoned the idea before signing anything. The later vendor relationship started from scratch.

Next step at the time: compare a few real schema options with Avery before writing any more code. That comparison became the schema decision in January.

#prototype
