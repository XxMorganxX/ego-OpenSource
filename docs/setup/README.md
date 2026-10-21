# Setting up ego-OS with an agent

This document is written **to the AI agent** (Claude Code, Codex, Cursor, or
similar) that is setting up ego-OS for its user. Agent: read it in full before
acting, then execute the phases in order. You do the work — installing, editing
config, running commands, waiting, checking. The user is asked exactly four
things, listed next, and nothing else unless a check fails and the fix needs
their input.

Human reading this? Point your agent at it, or in Claude Code run `/setup-kb`.
The hand-driven version of the same setup is the README's
[Quickstart](../../README.md#quickstart-your-own-notes). To try ego-OS on
invented demo notes first, see the README's
[Try it in 5 minutes](../../README.md#try-it-in-5-minutes-demo-data-no-models).

**What you will have at the end:** Postgres and the service fleet running on
this machine, a folder of the user's Markdown notes ingesting continuously, an
enrichment provider chosen and verified on real notes, the MCP server
registered in the user's agent, and the dashboard open. Everything beyond that
(more sources, reboot survival, public exposure) is a pointer in
[Phase 8](#phase-8--next-steps).

**Time:** 20–40 minutes wall clock, most of it downloads and first-run builds.

## How to use this document

Every phase has the same three parts:

- **Do** — commands to run, from the repository root unless a step says otherwise.
- **Check** — a command and the observation that means the phase succeeded. Do
  not move on until the check passes.
- **If not** — the known ways the check fails and the fix for each.

Rules for you, the agent, are in [Guardrails](#guardrails). Read them now.

## The four decisions

Ask these in **one** message, up front. Do not ask anything else in this run
unless a check fails and the fix needs the user.

1. **Notes folder.** Where is the folder of Markdown files to ingest? Any
   folder of `.md` files works — it does not need to be an Obsidian vault
   (Obsidian is a good way to manage such a folder, nothing more). If the user
   has none, offer to create `~/Notes` empty.
2. **Enrichment provider.** Enrichment (entities, concepts, classification) can
   run through **`claude-cli`** — the user's Claude Code subscription, the
   shipped default, spends quota on every note — or entirely **`local`** on
   Ollama — free and private, but needs ≥32 GB unified memory on Apple
   Silicon or a comparable discrete GPU, and is slower. Embeddings are always
   local either way.
3. **MCP client.** Which agent should be able to query the knowledge base:
   Claude Code (one command), or another MCP client (the user will need to
   know where that client declares MCP servers)?
4. **First-ingest size.** Ingest the whole folder now, or start with a **trial
   folder** of three to five copied notes to see the time and quota cost before
   committing the whole corpus? Recommend the trial when the folder has more
   than ~200 notes or the provider is `claude-cli`.

Record the answers; several phases branch on them.

## Phase 0 — Preflight (≈5 min, longer if installs are needed)

**Do**

```bash
uname -s                     # Darwin or Linux
docker --version && docker info --format '{{.ServerVersion}}'
uv --version
node --version               # v20 or newer
ollama --version
curl -sf http://127.0.0.1:11434/api/tags >/dev/null && echo ollama-up
```

Memory, for decision 2 (one command, works on both platforms):

```bash
case "$(uname -s)" in
  Darwin) echo "$(( $(sysctl -n hw.memsize) / 1073741824 )) GB" ;;
  Linux)  free -g | awk '/Mem:/{print $2 " GB"}' ;;
esac
```

If decision 2 is `claude-cli`:

```bash
claude --version
claude -p 'Reply with exactly: OK'
```

**Check**

Every command prints a version; `docker info` prints a server version (the
daemon is running, not just installed); `ollama-up` prints; Node is v20+. If
`claude-cli` was chosen, `claude -p` prints `OK`. If `local` was chosen, memory
is ≥32 GB or the user has confirmed a comparable GPU.

**If not**

- `uname` prints anything else (Windows, MSYS, WSL): stop. Only macOS and Linux
  are supported. Tell the user and end the run.
- A tool is missing: offer the install and run it **only after a yes**:
  - macOS (Homebrew): `brew install --cask docker-desktop` (older Homebrew:
    `--cask docker`; then open Docker.app once), `brew install uv`,
    `brew install node`, `brew install ollama`.
  - Linux: uv `curl -LsSf https://astral.sh/uv/install.sh | sh`; Ollama
    `curl -fsSL https://ollama.com/install.sh | sh`; Docker and Node 20+ via the
    distribution's package manager or their official docs (use `nvm` if the
    distro's Node is older than 20).
  - Claude Code CLI: follow the install steps at
    https://claude.com/product/claude-code.
- `docker info` fails with `permission denied` on `/var/run/docker.sock`
  (Linux): Docker is installed but the user is not in the `docker` group.
  `sudo usermod -aG docker "$USER"`, then log out and back in. Do not
  reinstall Docker.
- `ollama-up` does not print: Ollama is installed but not running. macOS: open
  Ollama.app (menu-bar icon) or `brew services start ollama`. Linux:
  `ollama serve` in a separate terminal, or `systemctl start ollama`. Re-check.
- `claude -p` asks to log in or errors with an auth message: the login is
  interactive and **you cannot do it**. Ask the user to run `claude` once in
  their own terminal, complete the login, then tell you. Re-check.
- Memory is below 32 GB and the user chose `local`: tell them plainly the
  12B chat model will be slow or fail to load, and offer `claude-cli` instead.

## Phase 1 — Configure (≈3 min)

Decision 1 gives the notes folder. If decision 4 is *trial*, first build the
trial folder and use **it** as the notes folder for now:

```bash
mkdir -p ~/Notes-trial
cp "<three to five .md files the user names>" ~/Notes-trial/
```

If the user asked you to create an empty folder: `mkdir -p ~/Notes`.

**Tell the user two things before the daemon ever runs** (no question, a
statement): (a) the ingest daemon writes a stable `id:` line into the
frontmatter of every note it ingests so identity survives renames — if they
object, you will set `KB_OBSIDIAN_STAMP=0` below and identity falls back to
the file path; (b) their notes appear in the knowledge tree under a top-level
folder named `Obsidian` by default — if they do not use Obsidian you will
name it `Notes` instead.

Shell variables do not survive between an agent's tool calls, so every block
in this guide that uses `$NOTES` sets it on its first line — substitute the
absolute path from decision 1 each time.

**Do**

```bash
NOTES="$HOME/Notes"            # or ~/Notes-trial, or the user's absolute path
cp KB_Core/.env.example KB_Core/.env
python3 - "$NOTES" <<'PY'
import pathlib, re, secrets, sys
p = pathlib.Path("KB_Core/.env"); s = p.read_text()
def setk(s, k, v):
    pat = re.compile(rf"^#?\s*{k}=.*$", re.M)
    return pat.sub(f"{k}={v}", s, count=1) if pat.search(s) else s.rstrip("\n") + f"\n{k}={v}\n"
s = setk(s, "POSTGRES_PASSWORD", secrets.token_hex(24))   # hex: never contains a quote
s = setk(s, "KB_APP_PASSWORD",   secrets.token_hex(24))
s = setk(s, "KB_INGEST_SECRET",  secrets.token_hex(32))
s = setk(s, "KB_OBSIDIAN_VAULT", sys.argv[1])   # despite the name, any Markdown folder
p.write_text(s)
PY
```

(`KB_OBSIDIAN_VAULT` is a historical name: the adapter ingests any folder of
Markdown files, Obsidian or not.)

Then, depending on what the user said above, append zero or more of:

```bash
echo 'KB_OBSIDIAN_LAYER=Notes' >> KB_Core/.env     # top folder label, if not an Obsidian user
echo 'KB_OBSIDIAN_STAMP=0'     >> KB_Core/.env     # only if the user objected to id: stamping
```

**Check**

```bash
grep -E '^(POSTGRES_PASSWORD|KB_APP_USER|KB_APP_PASSWORD|KB_INGEST_SECRET|KB_OBSIDIAN_VAULT)=' KB_Core/.env | sed -E 's/=.*/=<set>/'
grep -nE "^(POSTGRES_PASSWORD|KB_APP_PASSWORD|KB_TEST_PASSWORD)=.*'" KB_Core/.env
test -d "$(grep -E '^KB_OBSIDIAN_VAULT=' KB_Core/.env | cut -d= -f2-)" && echo notes-ok
git status --short KB_Core/.env
```

Expected: five `<set>` lines; the quote grep prints **nothing**; `notes-ok`;
`git status` prints nothing (`.env` is gitignored — if it shows up, stop and
find out why before continuing).

**If not**

- A key is missing: the example file's key line was renamed; open
  `KB_Core/.env.example`, find the key, and set it by hand.
- The quote grep prints a line: a password contains `'`. Regenerate it with
  `openssl rand -hex 24` — `db/init/02-roles.sh` splices passwords into
  single-quoted SQL, and a `'` breaks role creation on the one and only first
  init.
- `notes-ok` missing: the path is not absolute or does not exist. Fix the path
  (no `~` inside the file; write it out).

## Phase 2 — Database (≈2 min)

**Do**

```bash
(cd KB_Core && docker compose up -d)
```

**Check** (give it up to 30 s after `up`)

```bash
(cd KB_Core && docker compose ps)
(cd KB_Core && docker compose exec -T db pg_isready -U kb)
(cd KB_Core && docker compose exec -T db psql -U kb -d kb_mcp -tAc \
  "SELECT rolname FROM pg_roles WHERE rolname IN ('kb_app','kb_test') ORDER BY 1")
```

Expected: `kb_mcp_db` is `running`; `accepting connections`; two lines,
`kb_app` and `kb_test`. (If the user changed `POSTGRES_USER` or `KB_APP_USER`
in `.env`, substitute those values for `kb` and `kb_app`.)

**If not**

- Port `5433` already in use (compose fails to bind): set `DB_HOST_PORT=5434`
  (or any free port) in `KB_Core/.env` and run `docker compose up -d` again.
  No volume wipe needed.
- The roles query returns fewer than two rows: first init failed part-way.
  Look at `docker compose logs db --tail 50`. The init scripts run **only on a
  brand-new volume**, so fixing `.env` alone does nothing. Recover with
  `(cd KB_Core && docker compose down -v && docker compose up -d)` — this
  deletes the Postgres volume, which is safe **only now**, before anything has
  been ingested (see [Guardrails](#guardrails)).
- `docker compose` not found: Docker Desktop is installed but not started, or
  the Compose plugin is missing. Start Docker, re-check.

## Phase 3 — Enrichment routing (≈1 min)

Routing lives in `KB_Core/inference.toml`, one `[task.<name>]` block per task.
It is read once at process start, so this phase runs **before** the fleet.

**Do**

- Decision 2 = `claude-cli`: change nothing. The shipped file already routes
  seven tasks to `claude-cli`.
- Decision 2 = `local`: switch the seven `claude-cli` tasks and drop their
  `model =` overrides (a `claude-cli`-only setting). `doc_kind`,
  `describe_entity`, `merge_judge` and `embed` are already `local`; `embed`
  must stay so.

```bash
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("KB_Core/inference.toml"); s = p.read_text()
tasks = ["entity_gate", "discover_classes", "classify_memory", "extract_entities",
         "meaning_gate", "concept_label", "curate_parse"]
for t in tasks:
    m = re.search(rf"^\[task\.{t}\]\n.*?(?=^\[|\Z)", s, re.M | re.S)
    assert m, f"missing [task.{t}] block"
    block = m.group(0)
    new = re.sub(r'^provider\s*=\s*"claude-cli"[^\n]*$', 'provider = "local"', block, flags=re.M)
    new = re.sub(r'^model\s*=[^\n]*\n', '', new, flags=re.M)
    s = s.replace(block, new, 1)
p.write_text(s)
PY
```

**Check**

```bash
grep -c 'provider = "claude-cli"' KB_Core/inference.toml
grep -A2 '^\[task\.embed\]' KB_Core/inference.toml
uv run --project KB_Core python -c "import tomllib; tomllib.load(open('KB_Core/inference.toml','rb')); print('toml-ok')"
```

Expected: `7` for `claude-cli`, `0` for `local`; the `embed` block still says
`provider = "local"`; `toml-ok`.

**If not**

- `toml-ok` missing: the edit broke the file. `git checkout KB_Core/inference.toml`
  and redo the edit by hand: in each of the seven blocks set
  `provider = "local"` and delete the block's `model = ...` line.
- Count is neither 7 nor 0: a block was skipped. Open the file and finish by hand.

## Phase 4 — Start the fleet (5–15 min on first run)

`kb-run` is the foreground supervisor: it starts eight services, restarts any
that crash, and before starting `inference` pulls any Ollama model not yet
present: the chat model `.env` names (`OLLAMA_MODEL`) and the embedding model
`qwen3-embedding:0.6b`. First run also does each service's `uv sync`,
the dashboard's `npm install` and build, and the model pulls — the chat model
(`gemma4:12b-it-q8_0`, about 13 GB) is pulled regardless of decision 2 because
`merge_judge` runs locally in every configuration. Slow is normal; stuck is not.

**Do** (from the repository root; if your harness has a background-process
facility, use it instead of `nohup`)

```bash
nohup uv run --project KB_Core kb-run > "$HOME/kb-run.log" 2>&1 & echo $! > "$HOME/kb-run.pid"
```

**Check** — repeat every 30 s until all pass, up to 15 min:

```bash
for u in 8000/healthz 8001/health 8030/healthz 8091/; do
  curl -sf -o /dev/null "http://127.0.0.1:$u" && echo "$u ok" || echo "$u not yet"
done
curl -s -o /dev/null -w 'mcp 3000 http=%{http_code}\n' http://127.0.0.1:3000/mcp
tail -30 "$HOME/kb-run.log"
```

Expected: four `ok` lines; the MCP line shows any HTTP status other than `000`
(the server speaks streamable HTTP and rejects a bare GET, which is fine —
`000` means nothing is listening); the log tail is not repeating the same
service start over and over.

**If not**

- `8001/health` never comes up and the log shows `inference` restarting:
  Ollama is not running (Phase 0). Start it; `kb-run` recovers on its own.
  `8000/healthz` also probes inference, so it stays `not yet` until 8001 is up
  — fix inference first.
- A port is taken: `lsof -nP -iTCP:<port> -sTCP:LISTEN` shows the owner. Move
  ours by setting the matching key in `KB_Core/.env` — `KB_DASHBOARD_PORT`
  (8000), `KB_FRONTEND_PORT` (8091), `KB_INFERENCE_PORT` (8001),
  `KB_INGEST_PORT` (8030), `KB_MCP_PORT` (3000) — then
  `kill "$(cat "$HOME/kb-run.pid")"`, wait for it to exit, and start again. The
  frontend follows both its own `KB_FRONTEND_PORT` and `KB_DASHBOARD_PORT` (its
  `/api` proxy target), so a moved dashboard port keeps the UI wired up. If you
  move a port (or `DB_HOST_PORT` in Phase 2), use the new number in every later
  command and in the closing report.
- Still downloading after 15 min: `ollama list` shows which model is present.
  Large model, slow link — keep waiting, tell the user the size.
- `Traceback` in the log mentioning `inference.toml`: routing is incomplete
  (Phase 3). Fix the file, restart `kb-run`.
- Ollama runs somewhere other than `http://localhost:11434`: set
  `OLLAMA_BASE_URL` in `KB_Core/.env` (both `kb-run`'s model check and the
  inference server read it) and restart `kb-run`.
- The models are already pulled, or `kb-run`'s model check itself is in the way:
  set `KB_RUN_SKIP_PREFLIGHT=1` for that run to skip the check and the pulls.

## Phase 5 — First ingest (5–20 min, mostly waiting on enrichment)

The notes folder is scanned every scheduler tick (20 s), so ingest starts by
itself. If the folder is empty, write one seed note first so there is
something to see:

```bash
NOTES="$HOME/Notes"            # same absolute path as Phase 1
cat > "$NOTES/Getting started.md" <<'MD'
# Getting started

This folder is ingested into my ego-OS knowledge base. Every Markdown file here
becomes a memory; entities and concepts are derived from them automatically.
MD
```

**Check** — ingest first (within ~1 min):

```bash
grep -E 'pull obsidian: seen=' "$HOME/kb-run.log" | tail -3
(cd KB_Core && docker compose exec -T db psql -U kb -d kb_mcp -tAc \
  "SELECT count(*) FROM nodes WHERE source='obsidian' AND type='note'")
```

Expected: a `seen=N` line with `N` at least the number of `.md` files in
the folder, repeated every tick. `seen` counts notes *read*; `changed` counts the
ones actually written, so the first tick shows `changed=N` and later ticks
`changed=0` until a note is edited. One line per 20-second tick (the scheduler
heartbeat, `KB_TICK_SECONDS`) is the healthy steady state, not a loop. If
`KB_Inference` isn't up yet, or its Ollama is down, enrichment waits (a short `local`
pause, re-checked) instead of failing; notes that did fail for real can be re-queued with the dashboard's **Retry**
button or `POST /api/ingest/retry-failed`. The SQL count is at least the number of
`.md` files.

Then enrichment (set expectations: `claude-cli` a few minutes for five notes,
`local` one to three minutes **per note** on a 12B model):

```bash
curl -s http://127.0.0.1:8000/api/ingest/status
(cd KB_Core && docker compose exec -T db psql -U kb -d kb_mcp -tAc \
  "SELECT count(*) FILTER (WHERE n.doc_kind IS NOT NULL) AS classified,
          count(*) FILTER (WHERE e.entity_type = 'instance') AS entities
   FROM nodes n LEFT JOIN entities e ON e.node_id = n.id")
```

Expected: `.enrichment` shows `done` growing while `pending`/`processing`
shrink (a status with no rows is an absent key, not `0`); `.pauses` is `[]`;
after the wait, `classified > 0`. `entities` may legitimately stay `0`: an
instance is promoted only once a name recurs across two distinct memories
(`KB_PROMOTE_THRESHOLD`, default 2), so a seed note or a handful of trial
notes often produce none. Do not wait on it.

**If not**

- `seen=0` from the start and count is `0`: the folder path is wrong or
  holds no `.md` files. Fix `KB_OBSIDIAN_VAULT`, restart `kb-run`.
- `.pauses` is not empty: the provider is paused (quota exhausted or auth
  lapsed) — the `until`/`reason` say which. Quota resumes by itself at the
  stated time; auth needs the user to run `claude` again (Phase 0). Memories
  still ingest and embed meanwhile; only enrichment waits.
- `.enrichment.failed` is growing: `grep -iE 'enrich|provider' "$HOME/kb-run.log" | grep -iE 'error|fail|unavailable' | tail -20`.
  `ProviderUnavailable` with `claude-cli` means the CLI was never logged in.
  `inference` not up yet, or Ollama stopped behind it, is not a failure: it shows
  as a `local` entry in `.pauses` and resumes on its own. `inference call failed`
  with an HTTP 500 is a real model error for that note. Once the cause is
  fixed (Phase 4's `8001/health` check passes), re-queue the failures:
  `curl -s -X POST http://127.0.0.1:8000/api/ingest/retry-failed` prints
  `{"requeued": N}`, and `.enrichment.failed` drops on the next status check.
  The user can do the same from the dashboard's **Ingestion** page
  (**Retry *N* retryable**). Failed notes are never retried on their own.
- `.enrichment.done` stays absent or `0` for far longer than the expectation
  above with `local`: the model is loading or swapping (32 GB bar). `ollama ps`
  shows it.

**If decision 4 was the trial** and the user is satisfied with what they saw:
switch to the full folder now. Set `KB_OBSIDIAN_VAULT` in `KB_Core/.env` to the
real path, `kill "$(cat "$HOME/kb-run.pid")"`, start `kb-run` again (Phase 4
command), and re-run this phase's checks. Trial memories are pruned
automatically because their notes are no longer present in the source; the
`id:` lines stamped into the trial copies are harmless.

## Phase 6 — Connect the user's agent and open the dashboard (≈3 min)

**Do**

Decision 3 = Claude Code:

```bash
claude mcp add --transport http kb-mcp http://127.0.0.1:3000/mcp
claude mcp list
```

Decision 3 = another client: the server is **streamable HTTP**, no auth, at
`http://127.0.0.1:3000/mcp`. Add it where that client declares MCP servers;
clients that use the common `mcpServers` JSON shape take:

```json
{ "mcpServers": { "kb-mcp": { "type": "http", "url": "http://127.0.0.1:3000/mcp" } } }
```

Dashboard: `open http://127.0.0.1:8091` (macOS) or `xdg-open http://127.0.0.1:8091`
(Linux).

**Check**

`claude mcp list` shows `kb-mcp` (connected). Then, in a **new** session of the
chosen client, ask it to call the `search_memories` tool with a word that
appears in one of the ingested notes — it returns that note. The dashboard
shows the notes under the top-level folder from Phase 1.

**If not**

- The client cannot connect: Phase 4's MCP port check must pass first; if the
  port was moved, use the new port in the URL.
- `search_memories` returns nothing: ingest has not finished (Phase 5) or the
  word is not in any note. Try a word from a note title.

## Phase 7 — Keep it running

`kb-run` is running in the background from Phase 4 and keeps running until the
machine restarts or it is stopped:

```bash
kill "$(cat "$HOME/kb-run.pid")"      # clean stop: kb-run forwards the signal to every service
nohup uv run --project KB_Core kb-run > "$HOME/kb-run.log" 2>&1 & echo $! > "$HOME/kb-run.pid"   # start again
```

After a stop, re-run the Phase 4 port probe — every line should say `not yet`;
if one still answers, `pkill -f kb-svc.sh` finishes the job.

Postgres is separate: Docker keeps the `kb_mcp_db` container running
(`restart: unless-stopped`), and its data lives in a named Docker volume, so it
survives reboots as long as Docker starts. Making the **fleet** survive a
reboot (launchd on macOS, systemd on Linux) is described in
[Running at boot](../operations.md#running-at-boot) — leave it for a later
session unless the user asks now.

## Phase 8 — Next steps

Present these to the user in this order, as a list with one line each:

1. **Recommended — mirror docs from your git repos.** READMEs, plans, and specs
   are one of the densest sources you own, and they change as you work. Recipe:
   [`recipes/repo-docs.md`](recipes/repo-docs.md). Offer to set it up in a
   follow-up session.
2. **Optional — email.** Recipe: [`recipes/email.md`](recipes/email.md).
3. **Optional — push anything else in over HTTP.** Clippers, chat exports,
   custom scripts. Recipe: [`recipes/webhook.md`](recipes/webhook.md).
4. **Recommended — teach your agents reusable procedures.** The KB hosts a personal skill
   registry every connected agent shares. Recipe:
   [`recipes/skill-registry.md`](recipes/skill-registry.md).
5. **Exposing the MCP server or dashboard beyond this machine** is a separate,
   security-sensitive project: [Exposing ego-OS beyond localhost](../exposure.md).
   Tell the
   user plainly: the dashboard API serves every request as the owner unless
   `KB_DASHBOARD_AUTH=cf-access` is configured, so it must never be tunnelled
   or port-forwarded before the
   [Exposing the dashboard](../exposure.md#exposing-the-dashboard) steps are done.

Then deliver the closing report below.

## Guardrails

Rules for the agent, for the whole run:

- **Never commit `KB_Core/.env`.** It is gitignored; if `git status` ever
  shows it, stop and investigate.
- **Never print secret values** into the conversation. Print key names and
  `<set>`, as the Phase 1 check does.
- **Never run `docker compose down -v`** after Phase 5 has ingested anything
  without an explicit, separate confirmation from the user — it deletes every
  memory, entity, and concept.
- **Never start ingesting a large folder through `claude-cli`** without first
  stating that every note will spend subscription quota. Decision 4 exists for
  this.
- **Install nothing without a yes** for that specific install.
- **Stay inside the phases.** Anything this guide points elsewhere for is out
  of scope for this run; offer it as a follow-up.

## Closing report

End the run with a report in this shape, values filled in:

```
ego-OS is running.

Services (all on 127.0.0.1):
  dashboard   http://127.0.0.1:8091
  MCP server  http://127.0.0.1:3000/mcp   (registered in <client>)
  API 8000 · inference 8001 · ingest 8030 · Postgres 5433 (Docker, kb_mcp_db)

Configured:
  notes folder     <path>          (<N> notes ingested, <M> enriched so far)
  provider         <claude-cli | local>
  KB_Core/.env     POSTGRES_PASSWORD, KB_APP_PASSWORD, KB_INGEST_SECRET set (values not shown)
  id: stamping     <on | off>
  tree top folder  <Obsidian | Notes>

Operate:
  stop     kill "$(cat ~/kb-run.pid)"
  start    nohup uv run --project KB_Core kb-run > ~/kb-run.log 2>&1 & echo $! > ~/kb-run.pid
  log      ~/kb-run.log

Next: repo docs (recommended) · email (optional) · webhook (optional) — see Phase 8.
```
