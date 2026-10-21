# Operating ego-OS

How the services fit together once the [Quickstart](../README.md#quickstart-your-own-notes)
works: what runs where, how to keep it running, how to run a second isolated instance,
and the knobs that change behaviour. Commands run from the repository root unless a
step says otherwise.

- [Services and ports](#services-and-ports)
- [kb-run](#kb-run)
- [Running at boot](#running-at-boot)
- [Running a second instance](#running-a-second-instance)
- [Running without models](#running-without-models)
- [When enrichment fails](#when-enrichment-fails)
- [Adding your own sources](#adding-your-own-sources)
- [Per-install policy](#per-install-policy)
- [Embedding-model constraint](#embedding-model-constraint)
- [Health checks and backups](#health-checks-and-backups)
- [Tests and CI](#tests-and-ci)

## Services and ports

| Service | What it runs | Port |
|---|---|---|
| `backend` | `kb-dashboard-api` (FastAPI) | 8000 (`KB_DASHBOARD_PORT`) |
| `frontend` | `vite preview` serving the built dashboard (`KB_Frontend/dist`) | 8091 (`KB_FRONTEND_PORT`) |
| `inference` | `KB_Inference/server.py`: Ollama chat + embeddings | 8001 (`KB_INFERENCE_PORT`) |
| `ingest` | `kb-mcp-ingestd`: adapters, scheduler, push receiver | 8030 (`KB_INGEST_PORT`) |
| `reanalyze` | `kb-mcp-reanalyzed`: background re-analysis | — |
| `resolve` | `kb-mcp-entity-resolved`: describes new entities, re-embeds changed ones, stages merge suggestions | — |
| `consolidate` | `kb-mcp-consolidated`: rebuilds derived concepts once writes go quiet | — |
| `mcp` | `kb-mcp`, streamable HTTP: the local, unauthenticated MCP surface (`/mcp`) | 3000 (`KB_MCP_PORT`) |
| `mcp-public` | the same `kb-mcp` with mandatory auth, for a tunnel ([exposure](exposure.md)) | 3100 (`KB_MCP_PUBLIC_PORT`) |
| `healthcheck` | `kb-healthcheck`: a periodic one-shot probe with alerts | — |
| `backup` | `kb-backup run`: a periodic one-shot, `age`-encrypted `pg_dump` | — |
| `agent-gateway` | `kb-agent-gateway`: the surface for hosted `kb-agent`s (not needed for a single user) | 8040 (`KB_GATEWAY_PORT`) |

Every service binds `127.0.0.1`. Postgres runs under Docker Compose (service `db`) and
Ollama runs as its own process. Both are prerequisites the scripts wait for; they never
start or stop them.

Each service is `bash KB_Core/scripts/kb-svc.sh <name>` with its environment sourced
from `KB_Core/.env`. On first start a service creates its own environment (`uv sync`,
`npm install` + `npm run build`). The frontend builds `dist/` only when it is missing, so
after pulling frontend changes run `(cd KB_Frontend && npm run build)`.

## kb-run

`kb-run` is the foreground supervisor: it starts services, prefixes their output,
restarts any that exit (backoff capped at 30 s) and stops all of them on `Ctrl-C`.

```bash
uv run --project KB_Core kb-run                           # the default eight
uv run --project KB_Core kb-run --except frontend,mcp     # default set minus these
uv run --project KB_Core kb-run --only backend,mcp        # ONLY these: --only replaces the default set
```

The default eight are `backend frontend inference ingest reanalyze resolve consolidate
mcp`. `mcp-public` and `agent-gateway` are opt-in and need configuration first. Keep
`healthcheck` and `backup` out of `kb-run`: they are one-shots, and `kb-run` would re-run
them every few seconds (and `backup` retention keeps only the newest 14 dumps).

Before starting `inference`, `kb-run` pulls any missing Ollama model: the chat model
(`OLLAMA_MODEL`, default `gemma4:12b-it-q8_0`) and `qwen3-embedding:0.6b`. It asks the
Ollama at `OLLAMA_BASE_URL` (default `http://localhost:11434`). Set
`KB_RUN_SKIP_PREFLIGHT=1` to skip the check and the pulls.

## Running at boot

**macOS (launchd).** `KB_Core/scripts/kb-dev.sh` manages one LaunchAgent per service:

```bash
KB_Core/scripts/kb-dev.sh install     # write + load the agents (RunAtLoad + KeepAlive)
KB_Core/scripts/kb-dev.sh status      # state + health probe per service
KB_Core/scripts/kb-dev.sh logs [name] # tail ~/Library/Logs/kb/
KB_Core/scripts/kb-dev.sh restart [name…]
KB_Core/scripts/kb-dev.sh stop        # the database container and Ollama keep running
KB_Core/scripts/kb-dev.sh uninstall
```

Labels are `${KB_SERVICE_PREFIX:-com.kb}.<name>`. `install` loads all twelve services,
the opt-in ones included: `healthcheck` and `backup` as periodic one-shots
(`StartInterval`). A `mcp-public` without its auth settings exits at once and launchd
relaunches it every 10 seconds, so configure it first or boot it out:

```bash
launchctl bootout gui/$(id -u)/${KB_SERVICE_PREFIX:-com.kb}.mcp-public
```

The scripts default `KB_REPO`, `KB_FRONTEND`, `KB_INFERENCE` and `KB_MCP_DIR` to the
checkout they live in, so the agents work from any clone path. Set those only to point a
service at a directory outside its checkout; launchd does not read your shell's
environment.

**Linux (systemd).** There is no bundled installer. The simplest option is `kb-run`
inside `tmux`/`screen`, or as the single `ExecStart` of one plain unit. If you want
systemd to restart services one by one, use a template unit at
`/etc/systemd/system/kb@.service`:

```ini
[Unit]
Description=ego-OS KB service (%i)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
# Run as your own user, never root: kb-svc.sh reads $HOME (PATH, the claude CLI login).
User=youruser
Environment=HOME=/home/youruser
WorkingDirectory=/path/to/KB_Core
# kb-svc.sh defaults these four to its own checkout; spelling them out keeps the
# unit independent of where the script is invoked from.
Environment=KB_REPO=/path/to/KB_Core
Environment=KB_FRONTEND=/path/to/KB_Frontend
Environment=KB_INFERENCE=/path/to/KB_Inference
Environment=KB_MCP_DIR=/path/to/KB_MCP
ExecStart=/usr/bin/env bash /path/to/KB_Core/scripts/kb-svc.sh %i
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

then `systemctl enable --now kb@backend kb@frontend kb@inference kb@ingest kb@reanalyze
kb@resolve kb@consolidate kb@mcp`. For `healthcheck` and `backup`, use a systemd timer
that runs `kb-svc.sh healthcheck` / `kb-svc.sh backup` on an interval.

## Running a second instance

To run another ego-OS next to an existing one on the same machine (for example, the
[demo](../README.md#try-it-in-5-minutes-demo-data-no-models) beside your real notes),
give the second checkout its own `KB_Core/.env` with:

- `KB_COMPOSE_PROJECT`: the Docker Compose project name (default `kb_mcp`). A different
  value gives it its own Postgres container and data volume. Without it, a second
  checkout reattaches to the first one's database.
- `DB_HOST_PORT`, plus `KB_DASHBOARD_PORT`, `KB_FRONTEND_PORT`, `KB_INFERENCE_PORT`,
  `KB_INGEST_PORT`, `KB_MCP_PORT`: ports not used by the first instance.
- `KB_SERVICE_PREFIX` (macOS launchd only): a different label prefix, so the two sets of
  LaunchAgents don't replace each other.

The test suites default to `localhost:5433`. Point `KB_TEST_DATABASE_URL` at the second
instance's port before running them there (see [Tests and CI](#tests-and-ci)).

`(cd KB_Core && docker compose down -v)` deletes only the volume of the compose project
that `.env` names.

## Running without models

ego-OS ingests notes and answers keyword search with no Ollama and no Claude login. This
is what the [demo](../README.md#try-it-in-5-minutes-demo-data-no-models) uses:

- In `sources.toml` (`KB_SOURCES_CONFIG` points at another copy), `is_enrichable = false`
  for a source means its notes are never queued for enrichment: no LLM passes and no
  embeddings. [`examples/demo-sources.toml`](../examples/demo-sources.toml) does this.
- `KB_EMBED_ENABLED=0` turns off only the embedding step and keeps the LLM passes.
- Start without inference: `kb-run --only backend,frontend,ingest,mcp`.

What still works: ingest, the tree, the dashboard, and the MCP tools. `search_memories`
falls back to keyword-only results (`"confidence": {"level": "keyword"}`). Entities,
concepts and semantic search need enrichment.

## When enrichment fails

The dashboard's **Ingestion** page groups failures by cause. The **Retry *N* retryable**
button re-queues the ones a retry can fix; the API equivalent re-queues every failed
note:

```bash
curl -X POST http://127.0.0.1:8000/api/ingest/retry-failed
```

A provider that is not ready is a pause, not a failure: `inference` still starting,
Ollama stopped behind a running `inference` (it answers 503), a quota window or a
signed-out CLI. Enrichment resumes by itself (`/api/ingest/status` lists active pauses).

## Adding your own sources

ego-OS ships the ingest **contract**, not a connector for every service. Three ways in,
from simplest:

1. **Write Markdown into the notes folder.** Any process that drops a note there is a
   source; the next scan picks it up. The recipes in
   [`docs/setup/recipes/`](setup/recipes/) give the layout (`email.md` covers mail).
2. **Push over the webhook.** POST signed records to the ingest receiver
   ([`setup/recipes/webhook.md`](setup/recipes/webhook.md)).
3. **Write an adapter.** Implement the `Adapter` protocol in
   `KB_Core/src/kb_core/adapters/base.py`: a `source` name plus `fetch()`, which yields a
   `NodeRecord` per item. Adapters never touch the database or compute ids; the Obsidian
   and git adapters next to it are worked examples.

Per-source behaviour (enabled, `is_enrichable`, `period_seconds`) lives in
`KB_Core/sources.toml`. A sync that runs on a timer can join the supervised fleet: write
a small bash file that appends its name to `KB_EXTRA_SERVICES` and defines
`kb_svc_<name>`, then list it in `KB_SERVICE_EXTENSIONS` in `KB_Core/.env` (see the
"Service extensions" block in `KB_Core/scripts/kb-svc.sh`). `kb-run --only` and
`kb-dev.sh install` then treat it like a built-in service.

## Per-install policy

The knobs that are really your preferences (which memories fade and how fast, whether a
quiet project's docs lose their boost, retrieval recency, the entity categories offered
to the extractor, the worked examples in the prompts) live in `KB_Core/policy.toml`
(gitignored; copy `KB_Core/policy.toml.example`) and `KB_Core/prompts.local/`. The
shipped defaults keep every such mechanism off or neutral. From `KB_Core/` (the tool
reads `./policy.toml`), validate with `uv run kb-policy check` and inspect with
`uv run kb-policy show`. Full
guide: [`KB_Core/docs/policy-setup.md`](../KB_Core/docs/policy-setup.md).

## Embedding-model constraint

The `nodes.embedding` column is `vector(1024)`, the native width of
`qwen3-embedding:0.6b`, and every stored vector, index and query assumes it.
`inference.toml` therefore pins `[task.embed] provider = "local"`. Changing the
embedding model (`OLLAMA_EMBED_MODEL`) to one with another width breaks every stored
vector: plan a migration (a new `vector(N)` column) and a full re-embed
(`kb-embed-backfill`) before trusting search again.

## Health checks and backups

- **`kb-healthcheck`** probes the database, the services and the enrichment queue,
  writes `healthcheck-status.json` and alerts on problems: a macOS notification, and,
  if you set `KB_PUSHOVER_TOKEN` and `KB_PUSHOVER_USER`, a Pushover message (the alert
  text goes to `api.pushover.net`). Run it on an interval (launchd does this after
  `kb-dev.sh install`; default every 15 minutes, `KB_HEALTHCHECK_INTERVAL`).
- **`kb-backup run`** writes an `age`-encrypted `pg_dump` of each database. It needs
  `KB_BACKUP_AGE_RECIPIENT` (keep the private key off this machine) and keeps the
  newest 14 files in `KB_BACKUP_DIR`. Restore with
  `kb-backup restore <file> --into <app-role DSN> --identity <key file>`.

Hosting other people on one machine (`kb-tenant`, `agent-gateway`, `KB_Agent`) exists in
the code but is not a supported, documented setup in this release.

## Tests and CI

`KB_Core` and `KB_MCP` share the test database `kb_mcp_test` (role `kb_test`), which
`db/init/02-roles.sh` creates the first time the Postgres volume initialises. The suites
`TRUNCATE` their tables, so never point `KB_TEST_DATABASE_URL` at a database you care
about. `KB_Retrieval` uses its own database, `kb_mcp_test_retrieval`, so concurrent
runs can't wipe each other's rows. Create it once, then install its extensions as the
bootstrap superuser (`vector` and `ltree` are not trusted extensions):

```bash
PGPASSWORD=kb_test createdb -h localhost -p 5433 -U kb_test kb_mcp_test_retrieval
(cd KB_Core && docker compose exec -T db psql -U kb -d kb_mcp_test_retrieval \
  -c "CREATE EXTENSION IF NOT EXISTS ltree; CREATE EXTENSION IF NOT EXISTS vector;")
```

`.github/workflows/ci.yml` runs one `scripts/ci/<job>.sh` per job: `lint` (ruff),
`python` (the KB_Core, KB_Retrieval, KB_MCP and KB_Agent suites plus a structure-only
check of the golden eval sets), `kb-inference`, `evals-tests`, `frontend` and `gitleaks`.
Run a job locally the same way, for example
`KB_TEST_DATABASE_URL=<a disposable test DB> scripts/ci/python.sh`; it creates that
database and a second `_b` one for the tenant-isolation tests. Run by hand, those tests
skip themselves when `KB_TEST_DATABASE_URL_B` is unreachable, so a local pass is not the
full CI run.

pytest reads only `KB_TEST_DATABASE_URL` (and `_B`); it ignores `KB_TEST_PASSWORD`. If you
changed the test password or the Postgres port, set the URL explicitly. `02-roles.sh` creates
`kb_mcp_test_b` only on a fresh volume; on an existing install create it once as the bootstrap
superuser:

```bash
(cd KB_Core && docker compose exec -T db psql -U kb -d postgres \
  -c "CREATE DATABASE kb_mcp_test_b OWNER kb_test;")
(cd KB_Core && docker compose exec -T db psql -U kb -d kb_mcp_test_b \
  -c "CREATE EXTENSION IF NOT EXISTS ltree; CREATE EXTENSION IF NOT EXISTS vector;")
```
