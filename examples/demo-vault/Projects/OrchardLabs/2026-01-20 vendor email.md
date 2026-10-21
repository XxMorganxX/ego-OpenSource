---
date: 2026-01-20
type: email
tags: [fruit-inventory, vendor, cold-storage]
---
# Cold-Storage Vendor Email

From: Noor Haddad <noor.haddad@example.com>
To: Avery Quinn <avery.quinn@example.com>
Subject: Orchard Labs — cold storage terms confirmed

Hi Avery, confirming the terms we discussed: monthly capacity of 40 pallets, temperature logging every 15 minutes, and an API feed we can pull into the fruit-inventory service. Let Sam know when you want the feed schema.

A few practical details from our side:

- Pallets are received at the north dock between 6:00 and 14:00. Anything later waits for the next morning.
- Each pallet gets a Glacierline tag. Please send us your SKU and harvest batch for every pallet so the feed can carry both.
- The feed returns one row per pallet per reading: tag, cooler, temperature, humidity and timestamp.
- Our contract runs month to month until the first renewal date in February.

Looking forward to working with you.

Noor Haddad
Account manager, Glacierline Cold Storage

---

Vendor status: terms confirmed, contract active month to month.

Note to self ([[Avery Quinn]]): forwarded to [[Sam Rivera]] for the feed schema. This is the written record of the contract we announced at the [[2026-01-17 weekly sync|weekly sync]]. Integrating the feed is goal 2 on the [[2026-01-25 q1 roadmap|Q1 Roadmap]].

#vendor #cold-storage
