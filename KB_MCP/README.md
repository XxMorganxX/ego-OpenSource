# KB_MCP

The ego-OS MCP server: the tool surface an AI agent (Claude Code, claude.ai or any MCP
client) uses to search, read and edit your knowledge base. The layer is deliberately
thin. Every tool is a few lines over `kb_core.KnowledgeBase` (the engine in
[`KB_Core`](../KB_Core/README.md)), and `search_graph` delegates to the strategies in
[`KB_Retrieval`](../KB_Retrieval/README.md). Nothing here owns data or business logic.

All of it is one module, `src/kb_mcp/server.py` (`build_server` registers the tools,
`main` starts the server), plus `src/kb_mcp/auth.py` for the public listener.

## Tools

The server registers 32 tools: 18 read and 14 write. The table with one line per tool is
in the [root README](../README.md#what-your-agent-can-do). Each tool's full contract is
its docstring in `server.py`, which is also the description agents see.

- **Search:** `search_memories`, `search_graph`, `expand`
- **Read:** `get_memory`, `list_memories`, `find_entity`, `get_entity`,
  `entity_activity`, `list_concepts`, `concept`, `list_doc_kinds`,
  `list_merge_suggestions`
- **Write:** `add_memory`, `update_memory`, `move_memory`, `delete_memory`,
  `set_importance`, `sync_source`, `approve_merge`, `reject_merge`
- **Skills:** `search_skills`, `get_skill`, `get_skill_file`, `upsert_skill`,
  `delete_skill`, `review_skill`
- **Agents:** `search_agents`, `get_agent`, `upsert_agent`, `delete_agent`,
  `review_agent`
- **Review:** `list_pending_reviews`

A few behaviours apply to every tool:

- **Search confidence.** `search_memories` and `search_graph` return a `confidence`
  block next to the results, because a ranker always returns *something*. Without a
  reachable embedder, search falls back to keyword-only and says so
  (`"level": "keyword"`).
- **Untrusted-content framing.** Note text in a result (content, summary, snippet,
  excerpt, description) is wrapped in `<untrusted-ID>…</untrusted-ID>` markers with a
  random per-response id, so text inside a note cannot pose as instructions, except the
  skill registry, whose text is the user's own instructions and is served verbatim. The
  write tools strip the markers if an agent hands framed text back.
- **Public write limits.** `mcp-public` serves only the write tools named in
  `KB_MCP_PUBLIC_WRITE_TOOLS` (`all` covers every write tool except the library's skill and agent
  upsert/delete/review tools, which need `KB_MCP_PUBLIC_LIBRARY_WRITES=1`), never deletes a whole subtree, and allows each
  caller `KB_MCP_PUBLIC_WRITE_RATE` write calls (default `60/hour`). Note bodies over
  1 MiB are refused on both listeners.
- **Tool switches.** The dashboard can disable any tool for every client (behind
  `KB_DB_VIEWER_PASSWORD`). A disabled tool disappears from `list_tools` and refuses
  calls. If the switch table can't be read, reads stay open and writes are refused.
- **Tracing.** Search and read calls are recorded (`mcp_trace`) for the dashboard's
  retrieval pages; payload sizes are measured in bytes.

## Two listeners

| | `mcp` (local) | `mcp-public` |
|---|---|---|
| Port | 3000 (`KB_MCP_PORT`) | 3100 (`KB_MCP_PUBLIC_PORT`) |
| Bind | `127.0.0.1` | `127.0.0.1`, reached through a tunnel |
| Auth | none | mandatory: `cf-access` or `internal-secret` |
| For | agents on this machine | agents elsewhere, claude.ai |

Both are the same `kb-mcp` entry point, started by `KB_Core/scripts/kb-svc.sh` with
different settings. Auth is chosen by `KB_MCP_AUTH`, which `kb-svc.sh` sets from
`KB_MCP_PUBLIC_AUTH` only for `mcp-public`:

- `cf-access` checks the Cloudflare Access JWT on every request (signature against the
  team's keys, audience tag, email allowlist). It needs `KB_CF_ACCESS_TEAM_DOMAIN`,
  `KB_CF_ACCESS_AUD` and `KB_CF_ACCESS_EMAILS`.
- `internal-secret` does a constant-time check of the `X-KB-MCP-Internal` header set by
  a proxy you run. It needs `KB_MCP_INTERNAL_SECRET` (at least 32 characters).

A bad auth configuration exits at startup, and so does a public hostname without auth.
Auth failures return 401. Setup: [`docs/exposure.md`](../docs/exposure.md).
[`tunnel.sh`](tunnel.sh) runs a Cloudflare tunnel to the public listener only, and
refuses to start until that listener is configured.

## Running it

As part of the stack, `kb-run` starts `mcp` (see the root
[Quickstart](../README.md#quickstart-your-own-notes)). Register it with Claude Code:

```bash
claude mcp add --transport http kb-mcp http://127.0.0.1:3000/mcp
```

On its own, `kb-mcp` reads its settings from the environment:

| Variable | Default | Meaning |
|---|---|---|
| `KB_DATABASE_URL` | — | Postgres DSN (the `kb_app` role). Migrations run on start |
| `KB_MCP_TRANSPORT` | `stdio` | `stdio` (client spawns the server), `http` (streamable HTTP at `/mcp`) or `sse` |
| `KB_MCP_HOST` / `KB_MCP_PORT` | `127.0.0.1` / `3000` | HTTP bind |
| `KB_INFERENCE_URL` | unset | `KB_Inference` base URL for query embeddings; unset or unreachable means keyword-only search |
| `KB_OBSIDIAN_VAULT` | unset | Notes folder for the `sync_source` tool |
| `KB_MCP_POOL_MAX` | `8` | Connection pool size |

```bash
cd KB_MCP
KB_DATABASE_URL=postgresql://kb_app:<password>@localhost:5433/kb_mcp \
KB_MCP_TRANSPORT=http uv run kb-mcp
```

## Tests

```bash
cd KB_MCP && uv run pytest
```

The suite uses the `kb_mcp_test` database that the Postgres init scripts create
(`KB_TEST_DATABASE_URL`, default `postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test`)
and truncates its tables, so never point it at real data. `tests/test_tool_gate.py`
pins the tool inventory and its read/write split; `tests/test_auth.py` covers the public
listener's auth wall.
