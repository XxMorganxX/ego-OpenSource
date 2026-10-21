# Running Postgres in Docker

The knowledge base needs **Postgres 16 with `ltree` and `pgvector`**. `KB_Core/` ships a
`docker-compose.yml` that runs exactly that — the `pgvector/pgvector:pg16` image — with a
persistent named volume.

## 1. Install Docker Desktop

Download and install from <https://www.docker.com/products/docker-desktop/>, launch it,
and wait until the whale icon shows **"running"**. Verify in a terminal:

```bash
docker --version
docker compose version
```

## 2. Configure

```bash
cd /path/to/your/checkout/KB_Core
cp .env.example .env      # then set POSTGRES_PASSWORD and KB_APP_PASSWORD (required)
```

`POSTGRES_PASSWORD` and `KB_APP_PASSWORD` are **required**: they ship empty, and
`docker compose` refuses to start until both are set (`openssl rand -hex 24` makes a
good value for each).

Defaults: superuser `kb`, database `kb_mcp`, host port **5433**. The host port is only a
default — set `DB_HOST_PORT` in `KB_Core/.env` to publish on another one (it is always
bound to `127.0.0.1`), and use that port in every connection string below.

`kb` is the cluster's **bootstrap superuser** — break-glass only, never used by a
running service. Everything that actually talks to the database connects as the
unprivileged app role `kb_app` instead:

```
postgresql://kb_app:<KB_APP_PASSWORD>@localhost:5433/kb_mcp
```

## 3. Bring it up

```bash
docker compose up -d       # from KB_Core/
docker compose ps          # the 'db' service should report (healthy)
```

The named volume `kb_pgdata` persists your data across restarts. On first boot,
`db/init/01-extensions.sql` enables `ltree` + `vector`, and `db/init/02-roles.sh`
provisions the roles: `kb_app` (owner of `kb_mcp`) and `kb_test` (owner of a
`kb_mcp_test` database with the same extensions). Both init scripts run exactly
once, on an empty volume — a stranger's first `docker compose up -d` now gets a
working app role and test database with no manual role-creation step. **Tests
need no setup**: `kb_test`/`kb_mcp_test` already exist from first init, so
`uv run pytest` just works against the local default
(`postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test`).

`KB_APP_PASSWORD`/`KB_TEST_PASSWORD` must not contain a single quote — the init
script interpolates them into single-quoted SQL literals, and since it only runs
once, a password rejected mid-init needs `docker compose down -v` (wipes the
volume) before you can retry.

## 4. Create the schema and load your notes

The schema is created automatically the first time the app connects (migrations run on
startup). To populate from your Obsidian vault, run the sync CLI — it migrates, then
upserts every note (idempotent, safe to re-run):

```bash
export KB_DATABASE_URL=postgresql://kb_app:<KB_APP_PASSWORD>@localhost:5433/kb_mcp
uv run kb-mcp-sync --vault /path/to/your/obsidian-vault
#                          ^ confirm this is your actual vault path
```

You should see something like `[obsidian] upserted=N deleted=0 errors=0`.

## 5. Point the dashboard / MCP server at it

```bash
# API (terminal 1, from KB_Core/)
KB_DATABASE_URL=postgresql://kb_app:<KB_APP_PASSWORD>@localhost:5433/kb_mcp uv run kb-dashboard-api
# Frontend (terminal 2, also starting in KB_Core/; the UI lives in KB_Frontend/ of this repository)
cd ../KB_Frontend && npm install && npm run dev
```

Open the URL Vite prints (default <http://localhost:5173>) — you should now see your
notes in the tree and graph.

## Everyday commands

Run these from `KB_Core/` (compose finds `docker-compose.yml` and `.env` there):

```bash
docker compose up -d        # start
docker compose stop         # stop (keeps data)
docker compose down         # stop + remove the container (keeps the named volume)
docker compose down -v      # ALSO delete the volume — wipes all data
docker compose logs -f db   # tail the database logs
docker compose exec db psql -U kb_app -d kb_mcp   # a psql shell as the app role
```

Use `docker compose exec db …` rather than a hard-coded container name: the container is
named after the compose project.

## A second, isolated instance

The compose project name defaults to `kb_mcp` (container `kb_mcp_db`). Setting
`KB_COMPOSE_PROJECT` in `KB_Core/.env`, together with a different `DB_HOST_PORT`, runs a
second instance alongside the first with its own container (`<project>_db`) and its own
data volume.
