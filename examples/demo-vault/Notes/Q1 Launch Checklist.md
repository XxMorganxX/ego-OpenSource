---
type: checklist
created: 2026-01-26
tags: [todo, checklist, fruit-inventory]
---
# Q1 Launch Checklist

Everything that has to be true before we call the [[2026-01-25 q1 roadmap|Q1 Roadmap]] done. Owner of the list: [[Avery Quinn]].

## Schema (goal 1, Sam)

- [x] Migration PR reviewed — [[2026-01-12 pr review]]
- [x] Staging dry run, duplicates listed for the warehouse team
- [x] Unique constraint on batch and SKU in production
- [x] Old flat table set to read-only
- [ ] Old flat table deleted (waiting for the auditors, see [[2026-02-12 standup]])

## Cold-storage feed (goal 2, Avery)

- [x] Terms confirmed in writing — [[2026-01-20 vendor email]]
- [x] Feed pulled every 15 minutes
- [x] Staleness alert after the [[2026-02-02 postmortem|outage]]
- [x] Alert pages a person, not an inbox — [[2026-03-11 Adopt Larchwire for paging]]
- [x] Renewal signed

## Duplicate SKUs (goal 3, Sam)

- [x] Root cause found — [[2026-01-22 dup sku bug]]
- [x] Rerun procedure written — [[How to rerun the harvest import]]
- [x] Zero incidents since January 24

## Housekeeping

- [x] Release notes published — [[2026-02-05 release notes]]
- [ ] Weekly staging snapshot refresh ([[Dana Whitfield]], from the retro)
- [ ] Blog post finished or dropped

#todo
