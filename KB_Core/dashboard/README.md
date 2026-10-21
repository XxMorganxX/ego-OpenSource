# KB Dashboard

Two-pane viewer/editor for the knowledge base: file tree on the left, a 2D force
graph of the selected folder's full subtree on the right, with light editing.

## Architecture

- `api/` (here) — a thin FastAPI app over the existing `KnowledgeBase` core (no DB
  logic of its own).
- The web UI — a Vite + React + TypeScript single-page app — lives in the
  `KB_Frontend/` directory of this repository. It talks to this API only over HTTP
  (dev proxy `/api` → `http://127.0.0.1:8000`).

The subtree graph (`/api/subtree`) returns a `{ nodes, links }` payload whose links are
tree containment (`kind: "containment"`).

## Running (development)

To run the whole stack, follow the Quickstart in the [root README](../../README.md).
To work on the dashboard alone, run two processes from two directories of this repo:

```bash
# 1. API — terminal 1, from the repo root (reads KB_DATABASE_URL; default port 8000)
cd KB_Core
KB_DATABASE_URL=postgresql://kb_app:<KB_APP_PASSWORD>@localhost:5433/kb_mcp uv run kb-dashboard-api

# 2. Frontend dev server — terminal 2, from the repo root (proxies /api -> http://127.0.0.1:8000)
cd KB_Frontend
npm install   # first time only
npm run dev
```

Open the URL Vite prints (default http://localhost:5173).

## Tests

```bash
(cd KB_Core && uv run pytest tests/test_dashboard_api.py)   # backend (needs the test Postgres)
(cd KB_Frontend && npm test)                                # frontend (Vitest)
```

## Notes

- The API has no login by default: anyone who can reach it acts as the owner. Keep it
  on `127.0.0.1`. To reach it from another machine, put an identity check in front
  first — see "Exposing the dashboard" in [`docs/exposure.md`](../../docs/exposure.md)
  (`KB_DASHBOARD_AUTH=cf-access`).
- `KB_DASHBOARD_PORT` overrides the API port.
