# Exposing ego-OS beyond localhost

By default every ego-OS service binds `127.0.0.1`, and the local MCP server (port 3000)
and the dashboard (port 8091) have **no authentication**. That is fine on one machine and
wrong on any network. This page covers the two supported ways out: the authenticated
public MCP listener and a login-protected dashboard. The scripts assume a Cloudflare
Tunnel (`cloudflared`) with Cloudflare Access in front; any equivalent tunnel and
identity gate works if you set the same variables.

All settings below go in `KB_Core/.env`; restart the affected services after changing
them. See also [SECURITY.md](../SECURITY.md) for what is in scope for vulnerability
reports.

## Public MCP server

Expose the MCP server (so an agent elsewhere, or claude.ai, can reach it) only through
the `mcp-public` service on port 3100, never the bare `mcp` service on 3000. It needs:

1. **A tunnel to `mcp-public`.** [`KB_MCP/tunnel.sh`](../KB_MCP/tunnel.sh) forwards only
   to `mcp-public` and refuses to run until the auth settings below are in place.
2. **An auth mode.** `mcp-public` refuses to start unless both are set:
   - `KB_MCP_PUBLIC_AUTH`: either `cf-access` (validates Cloudflare Access JWTs; also
     needs `KB_CF_ACCESS_TEAM_DOMAIN`, `KB_CF_ACCESS_AUD`, `KB_CF_ACCESS_EMAILS`) or
     `internal-secret` (a shared header secret from a proxy you run; needs
     `KB_MCP_INTERNAL_SECRET`, at least 32 characters).
   - `KB_MCP_PUBLIC_HOSTNAME`: the public hostname your tunnel forwards, for example
     `kb-mcp.example.com`. Without it every authenticated request is rejected as a
     DNS-rebinding attempt (HTTP 421).

The local `mcp` service ignores both settings, and `kb-mcp` refuses to start with a
public hostname but no auth mode. Start the listener with
`uv run --project KB_Core kb-run --only mcp-public` (or alongside the rest).

## Exposing the dashboard

> **Warning: the dashboard API has no login of its own by default.** Unless
> `KB_DASHBOARD_AUTH=cf-access` is set, `kb-dashboard-api` serves **every request as the
> owner**: anyone who can reach it can read, edit and delete every memory. The
> frontend's `/api` proxy forwards straight to it, so tunnelling or port-forwarding the
> `frontend` service (8091) exposes the API too. Never expose the dashboard on a network
> you do not fully control until the steps below are done.

1. **Put an identity gate in front of it.** Create a Cloudflare Access (or equivalent)
   application that covers *every* path on the dashboard's hostname, including `/api/*`
   and the `/api/ws` WebSocket.
2. **Make the API verify that gate itself**, so a request that bypasses the edge is
   still refused:
   - `KB_DASHBOARD_AUTH=cf-access`
   - `KB_CF_ACCESS_TEAM_DOMAIN`: your Access team domain
   - `KB_CF_ACCESS_AUD_DASHBOARD`: the *dashboard* Access application's audience tag
     (not the MCP application's)

   Every API request except the `/healthz` probe then needs a valid Access JWT for an
   allowed email (the owner by default); anything else gets `401`/`403`.
3. **Allow your hostname.** Set `KB_DASHBOARD_ORIGINS` (extra origins allowed to make
   changes) and `KB_FRONTEND_ALLOWED_HOSTS` (hostnames `vite preview` answers for), for
   example `kb.example.com`.
4. **Then** tunnel the `frontend` service (never the API's port 8000 directly), restart
   `backend` and `frontend`, and check that the public `/api/me` asks you to sign in and
   that `http://127.0.0.1:8091/api/me` without an Access token returns `401`.

`kb-dashboard-api` refuses to start when `KB_DASHBOARD_ORIGINS`,
`KB_FRONTEND_ALLOWED_HOSTS` or `KB_API_ALLOWED_HOSTS` names a non-local hostname without
`KB_DASHBOARD_AUTH=cf-access`. If a different login gate in front of the API verifies
every request (a reverse proxy with its own auth, say), set `KB_DASHBOARD_AUTH=none` to
state that: the API then starts with a warning, still with no login of its own. Any other
value of `KB_DASHBOARD_AUTH` is refused, so a typo can't silently turn auth off.

## Owner-only tools

Two dashboard features act on the host rather than on your notes. Both are owner-only,
off unless you turn them on, and gated by a shared password (`KB_DB_VIEWER_PASSWORD`).
That password is a second factor, not authentication; five wrong guesses from one
address lock it for 15 minutes.

- The **Database** page, a read-only browser over every raw Postgres table (enabled by
  setting `KB_DB_VIEWER_PASSWORD`; the same password gates the MCP tool on/off
  switches).
- The **one-click Claude re-auth** button (macOS only), which opens a Terminal on the
  host running `claude auth login` (enabled by `KB_DASHBOARD_ALLOW_RELOGIN=1`; without
  it the banner just shows the command).

`KB_DASHBOARD_IDENTITY` names the actor stamped on dashboard actions (judgments, tool
toggles) if more than one person uses the dashboard. `KB_Core/.env.example` lists every
optional setting.
