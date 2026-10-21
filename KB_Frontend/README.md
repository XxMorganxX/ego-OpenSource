# KB Frontend

Web UI for the knowledge base: a Vite + React + TypeScript single-page app. Left
pane is the file tree, right pane is a 2D force graph (`react-force-graph-2d`) of the
selected folder's full subtree, with a right drawer to view/edit/delete a node.

To run the whole stack (database, engine, this UI), follow the Quickstart in the
[root README](../README.md); `kb-run` builds and serves this app for you. This file
covers working on the frontend itself.

## How it connects to the backend

The app is a pure HTTP client of the dashboard API — it has no DB logic. In dev,
Vite proxies `/api` → `http://127.0.0.1:8000` (see `vite.config.ts`; the port follows
`KB_DASHBOARD_PORT`). That API is the FastAPI app in `KB_Core/dashboard/api/`, in the
`KB_Core/` directory of this repository, started with `uv run kb-dashboard-api`.

**The dashboard API has no login of its own by default:** unless
`KB_DASHBOARD_AUTH=cf-access` is set, it serves every request as the owner. Keep it
(and this app's `/api` proxy) on `127.0.0.1`; to reach it from elsewhere follow
"Exposing the dashboard" in [`docs/exposure.md`](../docs/exposure.md) first.

## Running (development)

Two processes, from two directories of this repo:

```bash
# 1. API — terminal 1, from the repo root (reads KB_DATABASE_URL; default port 8000)
cd KB_Core
KB_DATABASE_URL=postgresql://kb_app:<KB_APP_PASSWORD>@localhost:5433/kb_mcp uv run kb-dashboard-api

# 2. This frontend dev server — terminal 2, from the repo root
cd KB_Frontend
npm install   # first time only
npm run dev
```

Open the URL Vite prints (default http://localhost:5173).

## Tests / build

```bash
npm test        # Vitest (jsdom; no server needed)
npm run build   # type-check + production bundle
```
