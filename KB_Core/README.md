# KB_Core

The ego-OS engine. KB_Core owns the Postgres database and every write to it: the schema
and migrations, the ingest daemons, LLM enrichment, the entity and concept layers,
importance and decay, and the dashboard's HTTP API. The other packages consume it:
[`KB_MCP`](../KB_MCP/README.md) and the dashboard API call `KnowledgeBase` in-process,
[`KB_Retrieval`](../KB_Retrieval/README.md) builds on its read primitives, and
[`KB_Inference`](../KB_Inference/README.md) is called *by* it over HTTP.

The design rules (stable ids, a thin MCP layer, a tree for containment and edges for
relationships) are in [`docs/PRINCIPLES.md`](docs/PRINCIPLES.md).

## Code map

Everything is under `src/kb_core/` unless noted.

| Area | Where | What it does |
|---|---|---|
| Core service | `knowledge_base.py` | `KnowledgeBase`: every read and write (nodes, edges, search, entities, concepts, merges, ingest log). The large one |
| Contract | `domain.py`, `validation.py`, `identity.py`, `paths.py`, `registry.py` | Dataclasses, write-time validation, deterministic `uuid5` ids and content hashes, `ltree` path helpers, node types |
| Database | `db.py`, `migrations/*.sql` | Connection pools and the ordered SQL migration runner |
| Sources | `adapters/` (`obsidian`, `git`, `webhook`, `vault`), `sync.py`, `cli.py` | Turn a source into `NodeRecord`s; upsert present items and prune the rest |
| Ingest | `ingest/` | The `kb-mcp-ingestd` daemon: scheduler, HMAC push receiver, transform pipeline, enrichment worker, entity promotion |
| Inference | `inference_routing.py`, `ingest/inference.py`, `ingest/claude_cli_client.py`, `ingest/anthropic_client.py` | Route each task to `local`, `claude-cli` or `anthropic` per `inference.toml`; the clients for each |
| Enrichment passes | `ingest/llm.py`, `enrichment.toml`, `prompts/`, `schemas/` | Config-driven LLM passes with enforced JSON output; `prompts.py` overlays per-install prompt files |
| Re-analysis | `analysis/` | `kb-mcp-reanalyzed`: revisits nodes whose inputs have moved |
| Entities | `entities.py`, `entity_resolve.py`, `entity_resolve_daemon.py`, `describe.py`, `merge_judge.py`, `meaning_judge.py` | Entity naming and placement, descriptions, near-duplicate merge suggestions, the "is this a meaningful entity" judge |
| Concepts | `concepts.py`, `consolidate.py`, `consolidate_daemon.py`, `concept_judge.py` | Leiden communities over the memory–entity graph, labelled into concepts |
| Ranking signals | `importance.py`, `aging.py`, `policy.py`, `confidence_signal.py` | Importance modifiers, content-age decay, per-install policy, search confidence |
| Dashboard API | `dashboard/api/` (outside `src/`) | `kb-dashboard-api`, FastAPI over `KnowledgeBase` ([README](dashboard/README.md)) |
| Operations | `run_stack.py`, `healthcheck.py`, `backup.py`, `scripts/` | `kb-run`, health probe and alerts, encrypted backups, the `kb-svc.sh` / `kb-dev.sh` service scripts |
| Hosting | `tenants.py`, `tenancy.py`, `tenant_cli.py`, `gateway/`, `llm_jobs.py`, `cf_access.py` | Multi-tenant hosting and the agent gateway (not needed for a single user) |
| Publishing | `publish/` | The allowlist export pipeline that produces the public mirror of this repository |

## Configuration files

| File | Purpose |
|---|---|
| `.env` (copy `.env.example`) | Secrets, paths, ports. Every service reads it through `kb-svc.sh` |
| `sources.toml` | Per-source toggles: `enabled`, `is_enrichable`, `period_seconds` (`KB_SOURCES_CONFIG` overrides the path) |
| `inference.toml` | Which provider serves each inference task (`KB_INFERENCE_ROUTING` overrides the path) |
| `enrichment.toml` | The ordered LLM passes and their prompt and schema files |
| `policy.toml` (copy `policy.toml.example`, gitignored) | Per-install decay, dormancy, taxonomy; see [`docs/policy-setup.md`](docs/policy-setup.md) |
| `docker-compose.yml`, `db/init/` | Postgres 16 + pgvector + ltree; roles and the test database on first init ([`docs/docker.md`](docs/docker.md)) |

## Commands

The `[project.scripts]` entry points, run with `uv run <name>` from `KB_Core/` or
`uv run --project KB_Core <name>` from the repository root:

| Command | What it does |
|---|---|
| `kb-run` | Foreground supervisor for the service fleet ([operations](../docs/operations.md#kb-run)) |
| `kb-mcp-ingestd` | Ingest daemon: scheduled pulls, push receiver, enrichment worker |
| `kb-mcp-reanalyzed` | Background re-analysis daemon |
| `kb-mcp-entity-resolved` | Entity describe / re-embed / merge-suggestion daemon |
| `kb-mcp-consolidated` | Concept-consolidation daemon |
| `kb-dashboard-api` | The dashboard's HTTP API |
| `kb-healthcheck` | One health probe with alerts (run it on an interval) |
| `kb-mcp-sync` | One-shot sync of the notes folder |
| `kb-consolidate` | One concept-consolidation pass; previews before writing |
| `kb-embed-backfill` | Embed every node that has no vector yet |
| `kb-policy` | `check` / `show` the per-install policy |
| `kb-reset-entities` | Wipe the derived entity layer and requeue memories |
| `kb-prune-instances`, `kb-denoise-instances`, `kb-entity-hygiene` | Entity-layer cleanup tools |
| `kb-turns-rebuild` | Recompute how MCP trace calls group into turns |
| `kb-backup` | `run` / `restore` encrypted per-database dumps |
| `kb-tenant`, `kb-agent-gateway` | Multi-tenant hosting |
| `kb-check-publishable`, `kb-publish`, `kb-import-pr` | The public-mirror export pipeline |

Most of these are run for you by `kb-run` (see `scripts/kb-svc.sh` for the exact
environment each service gets).

## Migrations

Schema changes are plain SQL files in `src/kb_core/migrations/`, applied in filename
order by `db.migrate()`. Every service applies pending migrations when it starts, and
`schema_migrations` records what has run. Add a new numbered file; never edit one that
has shipped.

## Tests

```bash
cd KB_Core && uv run pytest
```

The suite needs Postgres: it uses the `kb_mcp_test` database that `db/init/02-roles.sh`
creates (`KB_TEST_DATABASE_URL`, default
`postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test`) and truncates its tables, so
never point it at real data. CI runs it through `scripts/ci/python.sh`; see
[operations](../docs/operations.md#tests-and-ci).

## Docs

- [Design principles](docs/PRINCIPLES.md)
- [Ingestion architecture](docs/ingestion-architecture.md) and the
  [pipeline as built](docs/ingestion-pipeline-visualization.md)
- [Postgres in Docker](docs/docker.md)
- [Per-install policy](docs/policy-setup.md)
- [Dashboard API](dashboard/README.md)
- [LLM call map](../docs/llm-inference-map.md): every model call, its input and prompt
