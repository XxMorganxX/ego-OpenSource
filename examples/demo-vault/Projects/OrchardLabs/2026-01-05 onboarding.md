---
date: 2026-01-05
type: onboarding doc
tags: [fruit-inventory, onboarding]
---
# Onboarding: Orchard Labs fruit-inventory service

Welcome to the fruit-inventory service. Owners: [[Avery Quinn]] (product), [[Sam Rivera]] (engineering). The service tracks SKUs from harvest through cold storage to shipment. Staging and production run separate database instances.

## First week

1. Read the [[Fruit-Inventory Service]] project page and its timeline.
2. Ask [[Dana Whitfield]] on the platform team for staging access. Production access is read-only for everyone for the first month.
3. Skim [[How to rerun the harvest import]] so you know what the nightly import does before you touch it.
4. Sit in on the weekly sync.

## Glossary

- **SKU**: one sellable unit type, for example "Bosc pears, 18 kg crate".
- **Harvest batch**: everything picked from one orchard block on one day. Every SKU row should point back to a batch.
- **Pallet**: the physical unit that moves through cold storage.
- **Shrink**: fruit written off because it spoiled or was damaged.

## Where to ask

Questions about what we build go to Avery. Questions about how it is built go to Sam. Questions about environments, paging and access go to Dana.

#onboarding
