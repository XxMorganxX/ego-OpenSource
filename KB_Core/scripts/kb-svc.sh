#!/usr/bin/env bash
# kb-svc — exec one KB service with its environment (the launchd entry point).
#
#   kb-svc.sh backend|frontend|inference|ingest|reanalyze|adopt-cursors|resolve|consolidate
#             |mcp|mcp-public|agent-gateway|backup|healthcheck|<extension service>
#
# With KB_TENANT=<slug> (tenant plists only) ingest/reanalyze/resolve/consolidate run
# against that tenant's database and profiles — see "Tenant mode" below.
#
# KB_SVC_DRY_RUN=1 prints the resolved KB_* environment and the command, starting nothing
# (e.g. `KB_TENANT=alice KB_SVC_DRY_RUN=1 kb-svc.sh ingest`) — see "Dry run" below.
#
# Each ${KB_SERVICE_PREFIX:-com.kb}.<name> LaunchAgent (written by `kb-dev install`) runs this
# script. It sources KB_Core/.env, derives the same env kb-dev used to inject per
# Terminal tab, waits for the service's dependencies (Postgres for the DB-backed
# ones), then runs the real process as a supervised child — launchd KeepAlive
# restarts it after a crash, logout, or reboot-then-login.
#
# On dependency timeout it exits non-zero: launchd relaunches on its throttle, so
# a service that boots before Docker/Postgres just retries until the DB is there.
set -euo pipefail

# launchd PATH is minimal. ~/.local/bin is required: the enrichment provider is
# `claude-cli` (KB_INFERENCE_PROVIDER), which shells out to the `claude` binary.
# Prepending (not replacing) also covers the host-run case: a Terminal-launched
# `kb-dev` already has a real PATH, and we just need these two dirs ahead of it.
export PATH="/opt/homebrew/bin:${HOME:-}/.local/bin:$PATH"

# Every service has its own venv. An inherited VIRTUAL_ENV (the documented
# `uv run --project KB_Core kb-run` sets it to KB_Core/.venv) makes `uv pip install`
# install into THAT venv instead of the service's own, so KB_Inference's deps landed
# in KB_Core/.venv and inference crash-looped on `No module named 'fastapi'`.
unset VIRTUAL_ENV

# Default every path to the checkout this script lives in (symlinks resolved: kb-dev
# is usually installed as a symlink on PATH), so a clone works from any directory.
_kb_self="${BASH_SOURCE[0]}"
while [[ -L "$_kb_self" ]]; do
    _kb_link="$(readlink "$_kb_self")"
    [[ "$_kb_link" == /* ]] || _kb_link="$(dirname "$_kb_self")/$_kb_link"
    _kb_self="$_kb_link"
done
KB_ROOT="$(cd "$(dirname "$_kb_self")/../.." && pwd)"
KB_REPO="${KB_REPO:-$KB_ROOT/KB_Core}"
KB_FRONTEND="${KB_FRONTEND:-$KB_ROOT/KB_Frontend}"
KB_INFERENCE="${KB_INFERENCE:-$KB_ROOT/KB_Inference}"
KB_MCP_DIR="${KB_MCP_DIR:-$KB_ROOT/KB_MCP}"

cd "$KB_REPO"
KB_REPO="$PWD"            # absolute from here on: every path below is built from it
if [[ -f .env ]]; then set -a; source .env; set +a; fi

# One registry for every service, whatever its cwd: mcp/mcp-public run from KB_MCP, where
# a cwd-relative tenants.toml does not exist. Relative values from .env
# resolve against KB_Core, never against the service's own directory.
KB_TENANTS_CONFIG="${KB_TENANTS_CONFIG:-$KB_REPO/tenants.toml}"
[[ "$KB_TENANTS_CONFIG" == /* ]] || KB_TENANTS_CONFIG="$KB_REPO/$KB_TENANTS_CONFIG"
export KB_TENANTS_CONFIG

# --- Dry run -------------------------------------------------------------------
# KB_SVC_DRY_RUN=1 kb-svc.sh <service> resolves the environment exactly as a launch would
# (.env, tenant mode, the service's own exports), then prints the KB_* variables and the
# command instead of starting anything: no DB wait, no uv/npm, no process. Secrets print as
# <set> and DSN passwords as ***. This is how the tenant-mode environment is tested.
dry_run() { [[ "${KB_SVC_DRY_RUN:-0}" =~ ^(1|true|yes|on)$ ]]; }
if dry_run; then
  uv()  { printf '[kb-svc] dry run: skipping uv %s\n' "$*" >&2; }
  npm() { printf '[kb-svc] dry run: skipping npm %s\n' "$*" >&2; }
fi
print_env() {
  local name value
  printf 'KB_TENANT=%s\n' "$KB_TENANT"
  while IFS= read -r name; do
    [[ "$name" == KB_TENANT ]] && continue
    value="${!name}"
    case "$name" in
      *SECRET*|*PASSWORD*|*TOKEN*|*_KEY) value="<set>" ;;
      *) value="$(printf '%s' "$value" | sed -E 's#(://[^:/@]*):[^@]*@#\1:***@#')" ;;
    esac
    printf '%s=%s\n' "$name" "$value"
  done < <(compgen -e | grep '^KB_' | sort)
  printf 'EXEC=%s\n' "$*"
}

# Services connect as the APP role, never as the cluster's bootstrap superuser.
# POSTGRES_USER/POSTGRES_PASSWORD stay reserved for docker-compose bootstrap and
# break-glass psql: `kb` is the initdb role and Postgres refuses to strip SUPERUSER
# from it (ERROR: the bootstrap user must have the SUPERUSER attribute), so the only
# way to keep runtime off superuser is to run as a different role. kb_app owns every
# object in kb_mcp and is denied COPY TO/FROM PROGRAM.
# Falls back to POSTGRES_* so a machine with an older .env still starts.
# No literal credential at the end of either chain: an unset value now fails
# authentication loudly instead of silently trying a guessable default.
PGUSER="${KB_APP_USER:-${POSTGRES_USER:-}}"
PGPASS="${KB_APP_PASSWORD:-${POSTGRES_PASSWORD:-}}"
PGDB="${POSTGRES_DB:-kb_mcp}"
PGPORT="${DB_HOST_PORT:-5433}"
DBURL="postgresql://${PGUSER}:${PGPASS}@localhost:${PGPORT}/${PGDB}"

# --- Tenant mode -------------------------------------------------------------
# KB_TENANT=<slug> (set in the tenant's launchd plist by kb-dev) re-points this service at
# that tenant's database, policy and routing/source profiles (hybrid
# tenancy). Unset or "owner" = today's stack, byte for byte. Tenant daemons never
# listen (no push receiver) and have no local vault: notes arrive through the gateway.
KB_TENANT="${KB_TENANT:-owner}"
if [[ "$KB_TENANT" != "owner" ]]; then
  [[ -x .venv/bin/python ]] || uv sync
  # Resolve first, as an assignment, so an unknown slug (exit 2) aborts under set -e
  # instead of being swallowed by a process substitution — and clear the owner's values
  # .env may have exported, so a failed or partial lookup can never leave a tenant
  # daemon pointed at the owner's database or policy. The lookup always prints a
  # KB_POLICY_CONFIG (the tenant's registry file, else the shipped policy.tenant.toml), and
  # load_policy() refuses the ./policy.toml fallback under KB_TENANT, so the owner's
  # policy.toml and prompts.local/ in this cwd are unreachable either way.
  tenant_env="$(.venv/bin/python -m kb_core.tenants env "$KB_TENANT")"
  unset KB_DATABASE_URL KB_VAULT_LAYER KB_POLICY_CONFIG
  while IFS= read -r line; do
    [[ -n "$line" ]] && export "$line"
  done <<< "$tenant_env"
  DBURL="$KB_DATABASE_URL"
  export KB_INFERENCE_ROUTING="$KB_REPO/inference.tenant.toml" \
         KB_SOURCES_CONFIG="$KB_REPO/sources.tenant.toml" \
         KB_INGEST_RECEIVER=0
  # Connection floor (sized so many tenants fit under max_connections): one
  # idle connection per daemon pool instead of four, and an enrichment width matched to
  # the tenant agent's concurrency instead of the owner's .env value (8).
  export KB_POOL_MIN_SIZE=1 KB_ENRICH_CONCURRENCY=2
  unset KB_OBSIDIAN_VAULT
fi
# --- End tenant mode ---------------------------------------------------------

BACKEND_PORT="${KB_DASHBOARD_PORT:-8000}"
FRONTEND_PORT="${KB_FRONTEND_PORT:-8091}"
INFERENCE_PORT="${KB_INFERENCE_PORT:-8001}"
INGEST_PORT="${KB_INGEST_PORT:-8030}"
MCP_PORT="${KB_MCP_PORT:-3000}"     # the UNAUTHENTICATED local listener; never a tunnel origin
MCP_PUBLIC_PORT="${KB_MCP_PUBLIC_PORT:-3100}"   # the AUTHED public listener; the tunnel's only allowed origin

log() { printf '[kb-svc] %s\n' "$*" >&2; }

# Run the service as a CHILD of this bash, NOT via exec. macOS TCC grants
# Desktop-folder/Full Disk Access by code identity, and /bin/bash is the binary
# that holds the grant: an exec'd python/node replaces bash's identity and gets
# EPERM reading anything under ~/Desktop when launchd respawns the job, while a
# child inherits bash's TCC responsibility. TERM/INT are forwarded so bootout/kickstart stop the
# real service, and we exit with the child's status so KeepAlive sees crashes.
#
# TCC also keys on HOW the process is started: kernel shebang-exec of a script
# that LIVES in a protected folder such as ~/Desktop (the .venv/bin/* console
# stubs) makes the script the process's TCC identity, and
# the spawned python is then denied ALL Desktop reads — it dies on pyvenv.cfg
# at interpreter startup and crash-loops on the KeepAlive throttle, sometimes
# for many minutes per label. Exec'ing the interpreter directly with the stub
# as an ARGUMENT (`python .venv/bin/foo`, like inference's `python server.py`)
# keeps the interpreter binary as the identity and is reliably allowed. Never launch a Desktop-resident script via its shebang here.
run() {
  if dry_run; then print_env "$@"; exit 0; fi
  "$@" &
  local child=$!
  trap 'kill -TERM "$child" 2>/dev/null' TERM INT
  wait "$child"
  local status=$?
  wait "$child" 2>/dev/null || true
  exit "$status"
}

wait_for_db() {
  # Postgres is the Docker container ${KB_COMPOSE_PROJECT:-kb_mcp}_db (restart: unless-stopped): after a
  # reboot+login it can trail Docker Desktop by a minute or two.
  dry_run && return 0
  local tries=150 pg_isready=/opt/homebrew/opt/postgresql@16/bin/pg_isready
  for ((i = 0; i < tries; i++)); do
    if [[ -x "$pg_isready" ]]; then
      "$pg_isready" -h localhost -p "$PGPORT" -q && return 0
    else
      (: < "/dev/tcp/127.0.0.1/$PGPORT") 2>/dev/null && return 0
    fi
    sleep 2
  done
  log "Postgres not reachable on :$PGPORT after $((tries * 2))s; exiting for relaunch"
  return 1
}

# --- Service extensions --------------------------------------------------------
# KB_SERVICE_EXTENSIONS (KB_Core/.env) lists extra service files, colon-separated, absolute
# or relative to the checkout root. Each file appends its names to KB_EXTRA_SERVICES and
# defines kb_svc_<name> (dashes as underscores), which kb-svc.sh calls with cwd = KB_Core and
# the log/run/wait_for_db helpers in scope. This is how a source you build yourself (an
# email or chat sync that writes notes into the vault) runs under the same supervision as
# the built-in services without editing these scripts.
KB_EXTRA_SERVICES=()
load_service_extensions() {
  local ext
  local -a exts
  [[ -n "${KB_SERVICE_EXTENSIONS:-}" ]] || return 0
  IFS=: read -r -a exts <<< "$KB_SERVICE_EXTENSIONS"
  for ext in "${exts[@]}"; do   # non-empty here: bash 3.2 (launchd's /bin/bash) + set -u
                                # treats an empty "${arr[@]}" as unbound
    [[ -n "$ext" ]] || continue
    [[ "$ext" == /* ]] || ext="$KB_ROOT/$ext"
    if [[ ! -f "$ext" ]]; then
      # warn and skip: one bad entry must not take every service down with it
      printf '[kb] KB_SERVICE_EXTENSIONS: %s not found, skipped\n' "$ext" >&2
      continue
    fi
    # shellcheck source=/dev/null
    source "$ext"
  done
}
load_service_extensions

svc="${1:-}"
case "$svc" in
  backend)
    wait_for_db
    [[ -x .venv/bin/kb-dashboard-api ]] || uv sync
    # KB_INFERENCE_URL like every other service: /healthz and concept curation
    # (curate_parse) otherwise fall back to :8001, which after a KB_INFERENCE_PORT move is
    # some other install's server.
    export KB_DATABASE_URL="$DBURL" KB_DB_VIEWER_PASSWORD="${KB_DB_VIEWER_PASSWORD:-}" \
           KB_INFERENCE_URL="http://127.0.0.1:$INFERENCE_PORT"
    run .venv/bin/python .venv/bin/kb-dashboard-api
    ;;
  frontend)
    cd "$KB_FRONTEND"
    [[ -d node_modules ]] || npm install
    [[ -d dist ]] || npm run build
    # vite preview serves the built dist/ on :$FRONTEND_PORT (default 8091) — the
    # Cloudflare tunnel's origin for whatever public hostname the tunnel is
    # configured with. Rebuilds are explicit (npm run build), not per-boot.
    # vite.config.ts reads KB_FRONTEND_PORT (its own port) and KB_DASHBOARD_PORT
    # (the /api proxy target) from the environment, so both must be exported here;
    # a port/proxy change takes effect on the next frontend restart, no rebuild.
    # node exec'd directly (not the .bin/vite shebang stub) — see TCC note above.
    export KB_FRONTEND_ALLOWED_HOSTS="${KB_FRONTEND_ALLOWED_HOSTS:-}"
    export KB_FRONTEND_PORT="$FRONTEND_PORT" KB_DASHBOARD_PORT="$BACKEND_PORT"
    run node node_modules/vite/bin/vite.js preview
    ;;
  inference)
    cd "$KB_INFERENCE"
    # Gate on a real import, not on the venv existing: a venv created by an install that
    # then went elsewhere (or failed half-way) satisfies `-x .venv/bin/python`, so the
    # install would never be retried. --python pins the target to THIS venv whatever
    # VIRTUAL_ENV says.
    if ! { [[ -x .venv/bin/python ]] \
           && .venv/bin/python -c 'import fastapi, uvicorn, langchain_ollama' >/dev/null 2>&1; }; then
      log "installing inference deps into $KB_INFERENCE/.venv"
      [[ -x .venv/bin/python ]] || uv venv
      uv pip install --python .venv/bin/python -r requirements.txt
    fi
    # Loopback unless KB_INFERENCE_HOST says otherwise (server.py refuses a non-loopback
    # bind without KB_INFERENCE_ALLOW_REMOTE=1 + KB_INFERENCE_TOKEN). Set explicitly so a
    # stray HOST in the environment can never move the bind.
    export PORT="$INFERENCE_PORT" HOST="${KB_INFERENCE_HOST:-127.0.0.1}" \
           OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-16384}"
    run .venv/bin/python server.py
    ;;
  ingest)
    wait_for_db
    [[ -x .venv/bin/kb-mcp-ingestd ]] || uv sync
    # No default: an unset KB_INGEST_SECRET must fail loudly, never fall back to a
    # known key (anyone who could reach :8030 could then forge an ingest event).
    # Only when the push receiver runs: tenant daemons (KB_INGEST_RECEIVER=0) never
    # listen, so they neither need nor read the HMAC key. The key is exported only on
    # the receiver path, still with no shell default. "Off" is the same value set the
    # daemon's receiver_enabled() accepts (0|false|no|off, trimmed, any case).
    receiver="$(printf '%s' "${KB_INGEST_RECEIVER:-1}" | tr '[:upper:]' '[:lower:]' \
                | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [[ ! "$receiver" =~ ^(0|false|no|off)$ ]]; then
      if [[ -z "${KB_INGEST_SECRET:-}" ]]; then
        log "refusing to start ingest: KB_INGEST_SECRET is unset"
        log "  generate one with: openssl rand -hex 32   (then add it to KB_Core/.env)"
        exit 1
      fi
      export KB_INGEST_SECRET
    fi
    export KB_DATABASE_URL="$DBURL" \
           KB_OBSIDIAN_VAULT="${KB_OBSIDIAN_VAULT:-}" \
           KB_INGEST_PORT="$INGEST_PORT" \
           KB_INFERENCE_URL="http://127.0.0.1:$INFERENCE_PORT" \
           KB_INFERENCE_TIMEOUT="${KB_INFERENCE_TIMEOUT:-600}"
    run .venv/bin/python .venv/bin/kb-mcp-ingestd
    ;;
  reanalyze)
    wait_for_db
    [[ -x .venv/bin/kb-mcp-reanalyzed ]] || uv sync
    export KB_DATABASE_URL="$DBURL" \
           KB_OBSIDIAN_VAULT="${KB_OBSIDIAN_VAULT:-}" \
           KB_INFERENCE_URL="http://127.0.0.1:$INFERENCE_PORT" \
           KB_INFERENCE_TIMEOUT="${KB_INFERENCE_TIMEOUT:-600}"
    run .venv/bin/python .venv/bin/kb-mcp-reanalyzed
    ;;
  adopt-cursors)
    # One-shot deployment command. Run only while `kb-dev stop` keeps every vocabulary
    # writer down: the daemon applies pending migrations, adopts legacy cursors, then exits.
    wait_for_db
    [[ -x .venv/bin/kb-mcp-reanalyzed ]] || uv sync
    export KB_DATABASE_URL="$DBURL"
    run .venv/bin/python .venv/bin/kb-mcp-reanalyzed --adopt-only
    ;;
  resolve)
    # Entity-resolve loop: describes blank entities, re-embeds dirty ones, stages
    # near-duplicate merge suggestions. Talks to the local inference server only.
    wait_for_db
    [[ -x .venv/bin/kb-mcp-entity-resolved ]] || uv sync
    export KB_DATABASE_URL="$DBURL" \
           KB_INFERENCE_URL="http://127.0.0.1:$INFERENCE_PORT" \
           KB_INFERENCE_TIMEOUT="${KB_INFERENCE_TIMEOUT:-600}"
    run .venv/bin/python .venv/bin/kb-mcp-entity-resolved
    ;;
  consolidate)
    # Global concept consolidation: clusters live memories together with their active entity
    # instances, then persists both as concept members. The daemon polls a churn probe
    # (KB_CONSOLIDATE_POLL, default 60s) and fires once writes go quiet and enrichment has
    # drained; KB_CONSOLIDATE_INTERVAL (default: daily) survives as a max-staleness backstop.
    wait_for_db
    [[ -x .venv/bin/kb-mcp-consolidated ]] || uv sync
    export KB_DATABASE_URL="$DBURL" \
           KB_INFERENCE_URL="http://127.0.0.1:$INFERENCE_PORT" \
           KB_INFERENCE_TIMEOUT="${KB_INFERENCE_TIMEOUT:-600}"
    run .venv/bin/python .venv/bin/kb-mcp-consolidated
    ;;
  mcp)
    # The MCP transport (../KB_MCP): the agent-facing tool surface, served as MCP
    # streamable-http at http://127.0.0.1:MCP_PORT/mcp (KB_MCP_TRANSPORT=http in its
    # main()). Its own uv project + venv; python exec'd directly per the TCC note.
    wait_for_db
    cd "$KB_MCP_DIR"
    [[ -x .venv/bin/kb-mcp ]] || uv sync
    # .env is sourced with set -a: if it ever exported KB_MCP_AUTH directly, the
    # LOCAL listener would start rejecting local clients. The durable var is
    # KB_MCP_PUBLIC_AUTH, mapped only in the mcp-public case; unset the process var here
    # as a belt against future .env drift. KB_MCP_PUBLIC_HOSTNAME is likewise meant
    # only for mcp-public: on this unauthenticated listener it would let tunnel traffic
    # pass the Host check (and kb-mcp refuses to start with it set and no auth).
    unset KB_MCP_AUTH
    unset KB_MCP_PUBLIC_HOSTNAME
    [[ -f "$KB_REPO/policy.toml" ]] && export KB_POLICY_CONFIG="${KB_POLICY_CONFIG:-$KB_REPO/policy.toml}"
    export KB_DATABASE_URL="$DBURL" \
           KB_OBSIDIAN_VAULT="${KB_OBSIDIAN_VAULT:-}" \
           KB_MCP_TRANSPORT=http \
           KB_MCP_HOST="${KB_MCP_HOST:-127.0.0.1}" \
           KB_MCP_PORT="$MCP_PORT" \
           KB_INFERENCE_URL="http://127.0.0.1:$INFERENCE_PORT"
    run .venv/bin/python .venv/bin/kb-mcp
    ;;
  mcp-public)
    # The PUBLIC MCP listener: same kb-mcp entrypoint as `mcp`, but on :3100 with
    # mandatory request auth — the ONLY origin the Cloudflare tunnel may target
    # (whatever public hostname the tunnel forwards). The bare local listener on
    # :3000 must never be exposed. Refuse to start without an auth mode.
    wait_for_db
    cd "$KB_MCP_DIR"
    [[ -x .venv/bin/kb-mcp ]] || uv sync
    if [[ -z "${KB_MCP_PUBLIC_AUTH:-}" ]]; then
      log "refusing to start mcp-public: KB_MCP_PUBLIC_AUTH is unset"
      log "  cf-access:       set KB_MCP_PUBLIC_AUTH=cf-access + KB_CF_ACCESS_TEAM_DOMAIN/_AUD/_EMAILS"
      log "  internal-secret: set KB_MCP_PUBLIC_AUTH=internal-secret + KB_MCP_INTERNAL_SECRET"
      log "  (all in KB_Core/.env; see docs/exposure.md, section 'Public MCP server')"
      exit 1
    fi
    if [[ -z "${KB_MCP_PUBLIC_HOSTNAME:-}" ]]; then
      log "refusing to start mcp-public: KB_MCP_PUBLIC_HOSTNAME is unset"
      log "  set it in KB_Core/.env to the public hostname your tunnel forwards"
      exit 1
    fi
    [[ -f "$KB_REPO/policy.toml" ]] && export KB_POLICY_CONFIG="${KB_POLICY_CONFIG:-$KB_REPO/policy.toml}"
    export KB_DATABASE_URL="$DBURL" \
           KB_OBSIDIAN_VAULT="${KB_OBSIDIAN_VAULT:-}" \
           KB_MCP_TRANSPORT=http \
           KB_MCP_HOST=127.0.0.1 \
           KB_MCP_PORT="$MCP_PUBLIC_PORT" \
           KB_MCP_AUTH="$KB_MCP_PUBLIC_AUTH" \
           KB_MCP_PUBLIC_HOSTNAME="$KB_MCP_PUBLIC_HOSTNAME" \
           KB_INFERENCE_URL="http://127.0.0.1:$INFERENCE_PORT"
    run .venv/bin/python .venv/bin/kb-mcp
    ;;
  agent-gateway)
    # The only network surface a tenant's kb-agent talks to (behind the tunnel on its own
    # hostname + Access service tokens). Opens one pool per tenant from tenants.toml.
    wait_for_db
    [[ -x .venv/bin/kb-agent-gateway ]] || uv sync
    export KB_DATABASE_URL="$DBURL" \
           KB_GATEWAY_PORT="${KB_GATEWAY_PORT:-8040}"
    run .venv/bin/python .venv/bin/kb-agent-gateway
    ;;
  backup)
    # Nightly encrypted per-database dumps (StartInterval); one-shot like healthcheck.
    wait_for_db
    [[ -x .venv/bin/kb-backup ]] || uv sync
    export KB_DATABASE_URL="$DBURL"
    run .venv/bin/python .venv/bin/kb-backup run
    ;;
  healthcheck)
    # Periodic health probe + alerting. One-shot on a StartInterval: prints a
    # report, alerts on problems, writes healthcheck-status.json, rotates oversized logs,
    # then exits. No wait_for_db — a down DB is itself worth alerting on, and the probe times
    # out gracefully. Run as a bash child (interpreter direct, stub as arg), per the TCC note.
    [[ -x .venv/bin/kb-healthcheck ]] || uv sync
    export KB_DATABASE_URL="$DBURL" \
           KB_INFERENCE_URL="http://127.0.0.1:$INFERENCE_PORT"
    run .venv/bin/python .venv/bin/kb-healthcheck --rotate
    ;;
  *)
    ext_fn="kb_svc_${svc//-/_}"
    if [[ -n "$svc" ]] && declare -F "$ext_fn" >/dev/null; then
      "$ext_fn"
      exit $?
    fi
    echo "usage: kb-svc.sh backend|frontend|inference|ingest|reanalyze|adopt-cursors|resolve|consolidate|mcp|mcp-public|agent-gateway|backup|healthcheck${KB_EXTRA_SERVICES[*]:+|${KB_EXTRA_SERVICES[*]// /|}}" >&2
    exit 64
    ;;
esac
