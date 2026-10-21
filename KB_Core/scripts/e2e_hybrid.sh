#!/usr/bin/env bash
# Hybrid tenancy end to end: one tenant, one gateway, one kb-agent, a fake `claude`.
#
# Proves: vault push -> gateway -> inbox -> tenant node -> llm_jobs row -> claimed by the
# agent -> run through `claude -p` (scripts/fake-claude) -> reply posted -> node leaves the
# enrichment queue -> manifest prune.
#
# Required env (never production):
#   KB_TEST_DATABASE_URL        the owner-side test database (e.g. kb_mcp_test). Never
#                               written: it lends the tenant DSN only its host and port;
#                               the tenant connects as its own role kb_t_<slug>, which
#                               `kb-tenant create` makes. Its name must contain
#                               `test` and must not be kb_mcp, or the run refuses
#   KB_TEST_ADMIN_DATABASE_URL  bootstrap role on the same cluster: creates/drops kb_t_e2e*
#                               (passed to `kb-tenant create` and the cleanup only)
# Optional: KB_E2E_GATEWAY_PORT (default 18040), KB_E2E_ENRICH_TIMEOUT (seconds, default 300).
#
# Isolation: a mktemp registry (KB_TENANTS_CONFIG), a random e2e<digits> slug, the tenant
# database owned by its own role, embeddings off (KB_EMBED_ENABLED=0, so the inference server is
# never called), the push receiver off, the gateway on a non-production port, kb-agent
# config/state under a mktemp KB_AGENT_HOME and no LaunchAgent. The EXIT trap kills every
# process this script started and drops the tenant database, on success and on failure.
# Needs `uv sync --frozen` in KB_Core and KB_Agent first, and psql + curl on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
core="$PWD"
agent_dir="$(cd "$core/../KB_Agent" && pwd)"
core_py="$core/.venv/bin/python"
agent_py="$agent_dir/.venv/bin/python"
: "${KB_TEST_DATABASE_URL:?set KB_TEST_DATABASE_URL to the owner-side test database}"
: "${KB_TEST_ADMIN_DATABASE_URL:?set KB_TEST_ADMIN_DATABASE_URL to the bootstrap-role DSN}"
for py in "$core_py" "$agent_py"; do
  [[ -x "$py" ]] || { echo "FAIL: $py missing (run uv sync --frozen)"; exit 2; }
done
test_db="${KB_TEST_DATABASE_URL##*/}"
test_db="${test_db%%\?*}"
if [[ "$test_db" != *test* || "$test_db" == "kb_mcp" ]]; then
  echo "FAIL: KB_TEST_DATABASE_URL must name a test database (got '$test_db')"; exit 2
fi
# The admin DSN reaches only `kb-tenant create` and the cleanup, never the gateway, the
# daemon, the agent or the fake claude: held in an unexported variable from here on.
admin_url="$KB_TEST_ADMIN_DATABASE_URL"
unset KB_TEST_ADMIN_DATABASE_URL KB_ADMIN_DATABASE_URL
pq() { PGCONNECT_TIMEOUT=5 psql "$@"; }
port="${KB_E2E_GATEWAY_PORT:-18040}"
enrich_timeout="${KB_E2E_ENRICH_TIMEOUT:-300}"
if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "FAIL: port $port is already in use"; exit 2
fi

# Never inherit the owner's stack: these would point a daemon at owner files or databases.
unset KB_POLICY_CONFIG KB_OBSIDIAN_VAULT KB_VAULT_LAYER KB_TENANT KB_INGEST_SECRET \
      KB_INFERENCE_ROUTING KB_SOURCES_CONFIG KB_GATEWAY_URL
work="$(mktemp -d)"
export KB_TENANTS_CONFIG="$work/tenants.toml"
export KB_DATABASE_URL="$KB_TEST_DATABASE_URL"            # the owner = the test database
export KB_BACKUP_DIR="$work/backups"
export KB_GATEWAY_HOST=127.0.0.1 KB_GATEWAY_PORT="$port" KB_AGENT_JOB_TIMEOUT=120
export KB_AGENT_HOME="$work/agent"                        # kb-agent config.toml + state.sqlite
slug="e2e$RANDOM$RANDOM"
db="kb_t_$slug"
pids=()
passed=0
db_ours=0              # 1 once `kb-tenant create` may have run CREATE DATABASE for $db
started=$SECONDS
run_epoch="$(date +%s)"   # manifest generations are unix seconds

cleanup() {
  local rc=$?
  set +eu                  # bash 3.2: an empty pids[@] is "unbound" under -u
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
  for p in "${pids[@]}"; do
    for _ in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
  done
  if [[ -f "$KB_TENANTS_CONFIG" ]] && grep -q "^\[tenant\.$slug\]" "$KB_TENANTS_CONFIG"; then
    PGCONNECT_TIMEOUT=5 KB_ADMIN_DATABASE_URL="$admin_url" "$core_py" "$core/.venv/bin/kb-tenant" delete "$slug" --yes --no-backup >/dev/null
  fi
  left=0
  if (( db_ours )); then
    # Belt and braces: a run killed between CREATE DATABASE and the registry write leaves a
    # database the registry does not name. Only ever a database this run created: db_ours
    # is set after checking that the name was free, and cleared if create found it taken.
    PGCONNECT_TIMEOUT=5 KB_ADMIN_DATABASE_URL="$admin_url" "$core_py" -c 'import sys; from kb_core.tenant_cli import admin_dsn, drop_database; drop_database(admin_dsn(), sys.argv[1])' "$db"
    left="$(pq "$KB_TEST_DATABASE_URL" -XAtqc "select count(*) from pg_database where datname = '$db'")"
  fi
  if [[ "$left" != "0" ]]; then
    echo "FAIL: cleanup could not drop $db"; rc=1
  elif (( rc != 0 )); then
    for f in gateway ingestd agent; do
      [[ -f "$work/$f.log" ]] && { echo "--- $f.log (tail)"; tail -n 25 "$work/$f.log"; }
    done
  fi
  rm -rf "$work"
  if (( rc == 0 && passed == 1 )); then
    echo "cleanup: $db dropped, processes stopped (total $((SECONDS - started))s)"
    echo "E2E OK: push -> enrich via agent -> manifest prune"
  else
    echo "E2E FAILED (exit $rc): ${failmsg:-see above}"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

failmsg=""
fail() { failmsg="$*"; echo "FAIL: $*"; exit 1; }
phase() { echo "[e2e +$((SECONDS - started))s] $*"; }

# --- Tenant: database + registry entry (the onboarding bundle holds the secret: not echoed) ---
taken="$(pq "$KB_TEST_DATABASE_URL" -XAtqc "select count(*) from pg_database where datname = '$db'")"
[[ "$taken" == "0" ]] || fail "database $db already exists; refusing to create or drop it"
db_ours=1
if ! KB_ADMIN_DATABASE_URL="$admin_url" "$core_py" "$core/.venv/bin/kb-tenant" create "$slug" \
    --email "$slug@example.com" >"$work/create.log" 2>&1; then
  grep -q "already exists" "$work/create.log" && db_ours=0      # lost a race: not ours
  cat "$work/create.log"; fail "kb-tenant create"
fi
head -n 1 "$work/create.log"
tenant_env="$("$core_py" -m kb_core.tenants env "$slug")"
tenant_dsn="$("$core_py" -m kb_core.tenants dsn "$slug")"
q() { pq "$tenant_dsn" -XAtqc "$1"; }
phase "tenant $slug created ($db)"

# --- Gateway (owner env, as kb-svc.sh agent-gateway runs it) ---
"$core_py" "$core/.venv/bin/kb-agent-gateway" >"$work/gateway.log" 2>&1 &
pids+=($!)
# --- Tenant ingest daemon (kb-svc.sh tenant mode: tenant env, tenant profiles, no receiver) ---
(
  unset KB_DATABASE_URL KB_VAULT_LAYER KB_POLICY_CONFIG
  while IFS= read -r line; do [[ -n "$line" ]] && export "$line"; done <<< "$tenant_env"
  export KB_TENANT="$slug" KB_INFERENCE_ROUTING="$core/inference.tenant.toml" \
         KB_SOURCES_CONFIG="$core/sources.tenant.toml" KB_INGEST_RECEIVER=0 KB_EMBED_ENABLED=0
  exec "$core_py" "$core/.venv/bin/kb-mcp-ingestd"
) >"$work/ingestd.log" 2>&1 &
pids+=($!)

for _ in $(seq 1 60); do
  code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/v1/status" || true)"
  [[ "$code" == "401" ]] && break                         # up, and refusing unsigned calls
  kill -0 "${pids[0]}" 2>/dev/null || fail "gateway exited"
  sleep 0.5
done
[[ "$code" == "401" ]] || fail "gateway not answering on :$port (last HTTP $code)"
for _ in $(seq 1 120); do
  grep -q "scheduler up" "$work/ingestd.log" && break
  kill -0 "${pids[1]}" 2>/dev/null || fail "ingest daemon exited"
  sleep 0.5
done
grep -q "scheduler up" "$work/ingestd.log" || fail "ingest daemon never started its scheduler"
grep -q "push receiver disabled" "$work/ingestd.log" || fail "ingest daemon is serving the push receiver"
phase "gateway :$port and tenant ingest daemon up"

# --- Agent: fake claude first on PATH as `claude` (init resolves it with shutil.which) ---
vault="$work/vault"
mkdir -p "$vault/Notes" "$work/bin"
printf '# One\nhello\n' > "$vault/Notes/one.md"
printf '# Two\nbye\n' > "$vault/Notes/two.md"
ln -s "$core/scripts/fake-claude" "$work/bin/claude"
export PATH="$work/bin:$PATH"
secret="$("$core_py" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["tenant"][sys.argv[2]]["agent_secret"])' "$KB_TENANTS_CONFIG" "$slug")"
"$agent_py" -m kb_agent.cli init --gateway "http://127.0.0.1:$port" --tenant "$slug" \
  --secret "$secret" --vault "$vault" --no-launchd >/dev/null
unset secret
grep -q "^claude_path = \"$work/bin/claude\"" "$KB_AGENT_HOME/config.toml" \
  || fail "kb-agent init did not resolve claude to the fake"
"$agent_py" -m kb_agent.cli status | sed 's/^/  /'
"$agent_py" -m kb_agent.cli sync --full | sed 's/^/  sync: /'
phase "agent initialised and synced"

# --- Push -> node: the vault source drains its inbox every 20 s ---
for _ in $(seq 1 60); do
  n="$(q "select count(*) from nodes where source = 'vault'")"
  [[ "$n" == "2" ]] && break
  sleep 1
done
[[ "$n" == "2" ]] || fail "expected 2 vault notes after the push, got $n"
phase "2 vault notes landed"

# --- Node -> job -> agent -> reply -> enriched ---
# done OR failed: the fake model's replies need not satisfy every pass; what this proves is
# push -> job claimed by the agent -> reply posted -> node left the enrichment queue.
drive_jobs() {
  "$agent_py" - <<'PY'
import signal
signal.alarm(90)                       # a hung claim or claude run must not hang the e2e
from kb_agent.config import load_config, state_path
from kb_agent.loop import AgentLoop
from kb_agent.state import State
print(AgentLoop(load_config(), State(state_path())).jobs_once(wait=10), flush=True)
PY
}
deadline=$((SECONDS + enrich_timeout)); rounds=0
while :; do
  n="$(q "select count(*) from nodes where source = 'vault' and enrichment_status in ('done', 'failed')")"
  [[ "$n" == "2" ]] && break
  (( SECONDS < deadline )) || fail "expected 2 vault notes to leave the enrichment queue within ${enrich_timeout}s, got $n"
  rounds=$((rounds + 1)); drive_jobs >>"$work/agent.log" 2>&1 || true
done
answered="$(q "select count(*) from llm_jobs where status = 'done'")"
(( answered >= 1 )) || fail "no llm_jobs row was answered by the agent"
echo "  nodes: $(q "select string_agg(enrichment_status || '=' || n, ' ') from (select enrichment_status, count(*) n from nodes where source = 'vault' group by 1 order by 1) s")"
echo "  jobs:  $(q "select string_agg(task || ':' || status || '=' || n, ' ') from (select task, status, count(*) n from llm_jobs group by 1, 2 order by 1, 2) s")"
phase "both notes left the enrichment queue via the agent ($rounds agent claim rounds)"

# --- Manifest prune: the note leaves the vault AND the agent's local state, so the scan
# emits no delete event and only the manifest can remove it server-side ---
rm "$vault/Notes/two.md"
"$agent_py" -c 'import sys
from kb_agent.config import state_path
from kb_agent.state import State
s = State(state_path())
assert sys.argv[1] in s.known(), "agent state never recorded " + sys.argv[1]
s.forget(sys.argv[1])' "Notes/two.md"
out="$("$agent_py" -m kb_agent.cli sync --full)"
echo "  sync: $out"
[[ "$out" == *"'sent': 0"* && "$out" == *"'manifest': True"* ]] \
  || fail "second sync should post only the manifest: $out"
for _ in $(seq 1 90); do
  left="$(q "select count(*) from nodes where source = 'vault'")"
  [[ "$left" == "1" ]] && break
  sleep 1
done
[[ "$left" == "1" ]] || fail "manifest prune left $left vault notes"
[[ "$(q "select count(*) from nodes where source = 'vault' and content like '%hello%'")" == "1" ]] \
  || fail "the prune removed the wrong note"
# Generations are unix seconds, never below the last one + 1: the server's
# cursor is the agent's second manifest, which is at least this run's start time.
sent_gen="$("$agent_py" -c 'from kb_agent.config import state_path
from kb_agent.state import State
print(State(state_path()).get_kv("manifest_generation"))')"
cursor="$(q "select cursor from sync_state where source = 'vault'")"
[[ "$cursor" == "$sent_gen" && "$cursor" -ge "$run_epoch" ]] \
  || fail "vault manifest generation is $cursor, expected the agent's last ($sent_gen) >= $run_epoch"
phase "manifest prune removed the deleted note"
passed=1
