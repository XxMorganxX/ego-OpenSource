# ego-OS — Ingestion Pipeline, as built (KB_Core)

A code-accurate map of the ingestion pipeline **as implemented** in `KB_Core/src/kb_core/ingest/`
and `KB_Core/src/kb_core/adapters/`. Every box names a real module/class, so you can read a diagram
and jump straight to the file.

> **Companion docs:** design-time diagrams [`ingestion-architecture.md`](ingestion-architecture.md) · [`PRINCIPLES.md`](PRINCIPLES.md).
>
> All diagrams are [Mermaid](https://mermaid.js.org) and render inline on GitHub, Obsidian, and VS Code.

---

## 1. End-to-end: one pipeline, two capture paths, one observable log

Pull and push **capture** differently but funnel through the **same** transform pipeline,
the same `Ingestor`/`KnowledgeBase` core, and the same identity — then every write is
mirrored into the `ingest_log` audit table.

```mermaid
flowchart TB
    subgraph capture["CAPTURE — raw data onto this machine"]
        direction TB
        Obs[("Obsidian vault<br/>*.md")]
        Push[("Push source<br/>webhook sender")]
        Fetch["ObsidianAdapter.fetch()<br/>pull · full scan"]
        Recv["receiver.py<br/>POST /ingest/{source}<br/>HMAC verify · 202"]
        Inbox[("ingest_inbox<br/>durable local cache")]
    end

    subgraph ingest["INGEST — decode · transform · persist"]
        Sync["SyncService.sync()<br/>mirror-prune"]
        Drain["InboxDrainService.drain()<br/>upsert/delete · no prune"]
        Pipe["Pipeline<br/>record: NormalizeContent<br/>post: FlagEnrichment · RecomputeImportance"]
        Ing["Ingestor.upsert<br/>(DEFAULT_INGESTOR / per-source)"]
    end

    KB["KnowledgeBase<br/>upsert · delete · mark_enriched"]
    Nodes[("nodes")]
    Log[("ingest_log<br/>node_created · node_updated · enrichment_done")]
    Enr["EnrichmentWorker.run()<br/>drains enrichment_status='pending'"]

    Obs --> Fetch --> Sync
    Push -->|HTTPS| Recv --> Inbox --> Drain
    Sync --> Pipe
    Drain --> Pipe
    Pipe --> Ing --> KB
    KB --> Nodes
    KB -. "same txn as the write" .-> Log
    Nodes -. "pending" .-> Enr
    Enr --> KB
```

**Read it:** everything left of `KnowledgeBase` is the ingestion system. The only
differences between the two paths are *where records come from* (`fetch()` vs the inbox)
and *whether removed items are pruned* (pull mirror-prunes; push never does — it sees
deltas). `ingest_log` is written **inside the same transaction** as the node write, so it
can never drift from what actually landed.

---

## 2. Node lifecycle & the observability log

When each `ingest_log` row is emitted — the "what just happened to my data" view. An
idempotent re-sync (unchanged `content_hash`) deliberately logs **nothing**, keeping the
log signal-only.

```mermaid
sequenceDiagram
    autonumber
    participant P as Pipeline (sync/drain)
    participant KB as KnowledgeBase
    participant N as nodes
    participant L as ingest_log
    participant W as EnrichmentWorker

    P->>KB: ingestor.upsert(record)
    KB->>N: INSERT … ON CONFLICT (one txn)
    alt brand-new node
        KB->>L: node_created
    else content_hash changed
        KB->>L: node_updated
    else unchanged re-sync
        Note over KB,L: nothing logged (idempotent)
    end

    Note over P,KB: post-stage, once per batch
    P->>KB: flag_stale_enrichment(source)
    KB->>N: enrichment_status = 'pending'

    Note over W: later, on the enrichment tick
    W->>KB: claim_enrichment() → 'processing'
    W->>W: run registered enrichers
    W->>KB: mark_enriched(id)
    KB->>N: status='done', enriched_hash=content_hash
    KB->>L: enrichment_done
```

View it any time:

```sql
SELECT at, event, source, external_id, path FROM ingest_log ORDER BY at DESC LIMIT 50;
```

…or through the dashboard API, behind its auth: `GET /api/ingest/log?limit=50`.

---

## 3. Enrichment status machine

`content_hash` (not a timestamp) drives the flag, so re-syncing unchanged content is free
while any real edit re-queues the node.

```mermaid
stateDiagram-v2
    [*] --> none : node inserted
    none --> pending : content_hash ≠ enriched_hash (FlagEnrichment)
    pending --> processing : claim_enrichment (FOR UPDATE SKIP LOCKED)
    processing --> done : enrichers ok → mark_enriched (logs enrichment_done)
    processing --> failed : an enricher raised → mark_enrichment_failed
    done --> pending : content changes again
    failed --> pending : content changes again
```

---

## 4. The daemon: every source is a standard scheduled task

`build_jobs()` is the single place each source becomes a `ScheduledJob`; the daemon runs
the push receiver and the scheduler in one always-on process. Each job carries its **own
pipeline** (`cfg.pipeline`) — the per-adapter transformation seam. Source periods and
toggles come from `KB_Core/sources.toml` (see `sources.toml` for shipped values; a
`period_seconds` of `0` means always due, re-running as soon as the previous run
finishes); the full key list is in [`ingestion-architecture.md`](ingestion-architecture.md) §7.

```mermaid
flowchart LR
    subgraph daemon["kb-mcp-ingestd · one process"]
        direction TB
        Sched["IngestScheduler<br/>per-job periods · restart-safe<br/>(seeded from sync_state)"]
        subgraph jobs["build_jobs() — config.SourceConfig × N"]
            J1["obsidian (pull)<br/>SyncService.sync · [obsidian] period_seconds"]
            J2["webhook (push)<br/>InboxDrainService.drain · [webhook] period_seconds"]
            J3["enrichment<br/>EnrichmentWorker.run · KB_ENRICH_PERIOD (120s idle poll)"]
        end
        App["build_app()<br/>signed push receiver"]
    end
    Sched --> J1
    Sched --> J2
    Sched --> J3
```

---

## 5. Box → file map

Paths are relative to `KB_Core/src/kb_core/`.

| Diagram box | Lives in |
|---|---|
| `ObsidianAdapter.fetch()` | `adapters/obsidian/adapter.py` |
| `receiver.py` / `build_app` | `ingest/receiver.py` · `ingest/daemon.py` |
| `ingest_inbox` / `Inbox` | migration `0004` · `ingest/inbox.py` |
| `SyncService.sync()` | `sync.py` |
| `InboxDrainService.drain()` | `ingest/drain.py` |
| `Pipeline` + transforms | `ingest/pipeline.py` · `ingest/transforms.py` |
| `Ingestor` / `DEFAULT_INGESTOR` | `adapters/base.py` |
| `KnowledgeBase` (upsert · mark_enriched) | `knowledge_base.py` |
| `ingest_log` | migration `0005_ingest_log.sql` |
| `EnrichmentWorker` / `Enricher` | `ingest/enrichment.py` |
| `IngestScheduler` / `ScheduledJob` | `ingest/scheduler.py` |
| `SourceConfig` / `build_jobs` / daemon | `ingest/config.py` · `ingest/daemon.py` |
