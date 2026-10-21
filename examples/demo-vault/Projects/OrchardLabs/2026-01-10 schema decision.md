---
date: 2026-01-10
type: design doc
tags: [fruit-inventory, schema, design]
---
# Fruit DB Schema Decision

[[Avery Quinn]] and [[Sam Rivera]] reviewed three schema options for the fruit-inventory service today. We picked the normalized schema (Option B) because it keeps SKU history auditable across harvest batches. Sam will draft the migration; Avery owns the rollout plan. Orchard Labs' inventory API team should have the new tables live before the Q1 freeze.

## Options considered

- **Option A: keep the flat pallet table** from the [[2025-11-02 prototype notes|prototype]] and add columns. Cheapest, but it cannot represent a pallet split across batches.
- **Option B: normalized tables** for harvest batches, SKUs, pallets and locations, joined by ids. More work up front; every count can be traced to a batch.
- **Option C: one document per pallet** with nested history. Flexible, but reporting across pallets gets slow and inconsistent.

## Why B

Bramblecrest-style customers ask "which batch did this crate come from?" and Option B is the only one that answers it without guesswork. It also gives us a natural place for a unique key on batch and SKU later.

## Follow-ups

- Sam: migration PR, see [[2026-01-12 pr review|PR Review: SKU migration]].
- Avery: rollout plan and freeze date.

Logged in the [[Decision Log]].

#design-doc #schema
