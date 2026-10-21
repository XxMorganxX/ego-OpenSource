#!/usr/bin/env bash
# kb-dev — manage the KB stack as supervised launchd services.
#
#   kb-dev [start]          ensure Docker + Postgres + Ollama, then (re)start every service
#   kb-dev stop             stop all services (DB container + Ollama keep running)
#   kb-dev restart [name…]  restart the named services (default: all)
#   kb-dev status           launchd state + health probe per service
#   kb-dev logs [name]      tail the service logs (default: all)
#   kb-dev install          (re)write + (re)load the launchd agents
#   kb-dev uninstall        stop and remove the launchd agents
#
# Each service is a ${KB_SERVICE_PREFIX:-com.kb}.<name> LaunchAgent (RunAtLoad +
# KeepAlive) running scripts/kb-svc.sh, so it starts at login and is restarted
# automatically after a crash — no Terminal tabs, no manually re-run stack.
# Logs: ~/Library/Logs/kb/.
#
#   backend    kb-dashboard-api         :8000
#   frontend   vite preview (dist/)     :8091   (tunnel origin for the dashboard's public hostname)
#   inference  KB_Inference server.py   :8001
#   ingest     kb-mcp-ingestd           :8030
#   reanalyze  kb-mcp-reanalyzed        (no port)
#   resolve    kb-mcp-entity-resolved   (no port)
#   consolidate kb-mcp-consolidated      (no port; derived concepts)
#   mcp        kb-mcp (streamable-http)  :3000   (agent-facing MCP tool surface, /mcp)
#   mcp-public kb-mcp (streamable-http)  :3100   (authed public listener, /mcp)
#   healthcheck kb-healthcheck           (no port; periodic health probe + alerts, StartInterval)
#   agent-gateway kb-agent-gateway       :8040   (KB_GATEWAY_PORT; tenant kb-agent surface)
#   backup     kb-backup run             (no port; encrypted per-DB dumps, StartInterval daily)
#
# Per enabled tenant in tenants.toml, kb-dev also supervises
# ${KB_SERVICE_PREFIX:-com.kb}.t.<slug>.{ingest,reanalyze,resolve,consolidate}
# (KB_TENANT=<slug>; logs ~/Library/Logs/kb/t.<slug>.<name>.log).
#
# Managed elsewhere (not by kb-dev):
#   database   Docker container kb_mcp_db (restart: unless-stopped) — returns with
#              Docker Desktop, which should have "Start when you sign in" enabled
#   ollama     brew services (homebrew.mxcl.ollama LaunchAgent) when Ollama came from
#              Homebrew — `kb-dev start` registers it if it isn't already; otherwise
#              Ollama.app or your own `ollama serve`
#   tunnel     not managed here: run cloudflared (or any other edge) as its own
#              service if you expose the MCP listener — kb-dev never starts or
#              kills a connector
#
# Override the repo location with: KB_REPO=/path/to/KB_Core kb-dev
set -euo pipefail

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
KB_SVC="$KB_REPO/scripts/kb-svc.sh"
SERVICES=(backend frontend inference ingest reanalyze resolve consolidate mcp mcp-public healthcheck agent-gateway backup)
# Per-tenant daemon set: one ${KB_SERVICE_PREFIX}.t.<slug>.<name> agent per enabled tenant in
# tenants.toml, each running kb-svc.sh <name> with KB_TENANT=<slug> (hybrid tenancy).
TENANT_SERVICES=(ingest reanalyze resolve consolidate)
UID_NUM="$(id -u)"
AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/kb"
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"

cd "$KB_REPO"

# Load KB_Core/.env (if present) so KB_SERVICE_PREFIX (and anything else it sets)
# reaches label()/plist() below and every service kb-dev supervises.
if [[ -f .env ]]; then set -a; source .env; set +a; fi
# The Postgres container docker-compose.yml names: <KB_COMPOSE_PROJECT>_db (default kb_mcp_db).
KB_DB_CONTAINER="${KB_COMPOSE_PROJECT:-kb_mcp}_db"

# Docker context: every docker/compose call uses the CLI's current context unless
# KB_DOCKER_CONTEXT (shell or KB_Core/.env) pins one. Pin it on a host that runs more than
# one docker daemon (e.g. colima alongside Docker Desktop): if the current context points
# at a different daemon than the one holding kb_mcp_db, `kb-dev start` would create an
# EMPTY duplicate kb_mcp_db there (its :5433 bind losing to the real one) and
# `kb-dev status` would report the decoy's health.
if [[ -n "${KB_DOCKER_CONTEXT:-}" ]]; then export DOCKER_CONTEXT="$KB_DOCKER_CONTEXT"; fi

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
SERVICES+=(${KB_EXTRA_SERVICES[@]+"${KB_EXTRA_SERVICES[@]}"})

# The same absolute registry path kb-svc.sh exports: never cwd-relative.
KB_REPO="$PWD"
KB_TENANTS_CONFIG="${KB_TENANTS_CONFIG:-$KB_REPO/tenants.toml}"
[[ "$KB_TENANTS_CONFIG" == /* ]] || KB_TENANTS_CONFIG="$KB_REPO/$KB_TENANTS_CONFIG"
export KB_TENANTS_CONFIG

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
label() { echo "${KB_SERVICE_PREFIX:-com.kb}.$1"; }
plist() { echo "$AGENT_DIR/${KB_SERVICE_PREFIX:-com.kb}.$1.plist"; }
tlabel() { echo "${KB_SERVICE_PREFIX:-com.kb}.t.$1.$2"; }
tplist() { echo "$AGENT_DIR/$(tlabel "$1" "$2").plist"; }

# Enabled tenant slugs from tenants.toml (none when the file is absent). `tenant_slugs all`
# also lists disabled tenants: stop/uninstall/status/restart use it, so a tenant disabled
# after install still has its daemons stopped; install/start launch enabled tenants only.
tenant_slugs() {
  [[ -f "${KB_TENANTS_CONFIG:-tenants.toml}" && -x .venv/bin/python ]] || return 0
  .venv/bin/python - "${1:-}" <<'PY'
import sys
from kb_core.tenants import load_registry
include_disabled = len(sys.argv) > 1 and sys.argv[1] == "all"
for t in load_registry().tenants(include_owner=False, include_disabled=include_disabled):
    print(t.slug)
PY
}

write_tenant_plist() {
  local slug="$1" name="$2"
  cat > "$(tplist "$slug" "$name")" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$(tlabel "$slug" "$name")</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$KB_SVC</string>
        <string>$name</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/t.$slug.$name.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/t.$slug.$name.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>KB_TENANT</key>
        <string>$slug</string>
    </dict>
</dict>
</plist>
PLIST
}

write_plist() {
  local name="$1"
  # Daemons are long-running (KeepAlive); the healthcheck (default 15 min) and the
  # backup (default daily) are periodic one-shots (StartInterval). All just run
  # scripts/kb-svc.sh <name>.
  local schedule="    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>"
  if [[ "$name" == "healthcheck" ]]; then
    schedule="    <key>StartInterval</key>
    <integer>${KB_HEALTHCHECK_INTERVAL:-900}</integer>"
  elif [[ "$name" == "backup" ]]; then
    schedule="    <key>StartInterval</key>
    <integer>${KB_BACKUP_INTERVAL:-86400}</integer>"
  fi
  cat > "$(plist "$name")" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$(label "$name")</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$KB_SVC</string>
        <string>$name</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
$schedule
    <key>StandardOutPath</key>
    <string>$LOG_DIR/$name.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/$name.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST
}

loaded() { launchctl print "gui/$UID_NUM/$(label "$1")" >/dev/null 2>&1; }
tloaded() { launchctl print "gui/$UID_NUM/$(tlabel "$1" "$2")" >/dev/null 2>&1; }

cmd_install() {
  mkdir -p "$LOG_DIR"
  for name in "${SERVICES[@]}"; do
    write_plist "$name"
    launchctl bootout "gui/$UID_NUM/$(label "$name")" 2>/dev/null || true
    launchctl enable "gui/$UID_NUM/$(label "$name")" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" "$(plist "$name")"
    bold "    loaded $(label "$name")"
  done
  for slug in $(tenant_slugs); do
    for name in "${TENANT_SERVICES[@]}"; do
      write_tenant_plist "$slug" "$name"
      launchctl bootout "gui/$UID_NUM/$(tlabel "$slug" "$name")" 2>/dev/null || true
      launchctl enable "gui/$UID_NUM/$(tlabel "$slug" "$name")" 2>/dev/null || true
      launchctl bootstrap "gui/$UID_NUM" "$(tplist "$slug" "$name")"
      bold "    loaded $(tlabel "$slug" "$name")"
    done
  done
}

ensure_docker() {
  bold "==> Checking Docker…"
  if ! docker info >/dev/null 2>&1; then
    if [[ "$(uname -s)" == Darwin ]] && command -v open >/dev/null 2>&1; then
      bold "    Docker isn't running — launching Docker Desktop…"
      open -a Docker || {
        echo "Could not launch Docker Desktop. Start your docker daemon, then re-run kb-dev." >&2
        exit 1
      }
    else
      echo "Docker isn't running. Start your docker daemon, then re-run kb-dev." >&2
      exit 1
    fi
    for _ in {1..60}; do
      docker info >/dev/null 2>&1 && break
      sleep 2
    done
    docker info >/dev/null 2>&1 || {
      echo "Docker did not come up in ~2 min. Start Docker Desktop manually, then re-run kb-dev." >&2
      exit 1
    }
  fi
  bold "    Docker is up."
}

ensure_db() {
  bold "==> Bringing up Postgres (docker compose up -d)…"
  docker compose up -d
  local status=""
  for _ in {1..30}; do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$KB_DB_CONTAINER" 2>/dev/null || echo none)"
    [[ "$status" == healthy ]] && break
    sleep 2
  done
  if [[ "$status" != healthy ]]; then
    echo "Database never became healthy (last status: $status). Inspect with: docker compose logs db" >&2
    exit 1
  fi
  bold "    Database healthy."
}

ensure_ollama() {
  bold "==> Checking Ollama…"
  # Homebrew's ollama gets a login service; any other install (Ollama.app, a manual
  # `ollama serve`) is left alone and only probed below.
  if command -v brew >/dev/null 2>&1 && brew list --formula ollama >/dev/null 2>&1; then
    if ! brew services list 2>/dev/null | grep -E '^ollama\s+started' >/dev/null; then
      bold "    Registering ollama with brew services (login LaunchAgent + KeepAlive)…"
      brew services start ollama
    fi
  fi
  local up=0
  for _ in {1..30}; do
    curl -fsS "$OLLAMA_BASE_URL/api/tags" >/dev/null 2>&1 && { bold "    Ollama is up."; up=1; break; }
    sleep 1
  done
  if [[ "$up" != 1 ]]; then
    echo "    WARNING: Ollama unreachable at $OLLAMA_BASE_URL — inference stays unhealthy until it is." >&2
    return
  fi

  # Pull required models if missing — a fresh machine otherwise reaches
  # "ollama is up" with a permanently unhealthy inference service.
  for model in "${OLLAMA_MODEL:-gemma4:12b-it-q8_0}" "qwen3-embedding:0.6b"; do
    if ! curl -fsS "$OLLAMA_BASE_URL/api/tags" | grep -q "\"${model}\""; then
      bold "pulling ollama model ${model} (first run only — this can take a while)"
      ollama pull "$model"
    fi
  done
}

cmd_start() {
  ensure_docker
  ensure_db
  ensure_ollama
  bold "==> Starting services…"
  local need_install=0
  local slugs
  # A broken tenants.toml must not stop the owner stack from starting.
  slugs="$(tenant_slugs)" \
    || { echo "    WARNING: tenants.toml unreadable — tenant daemons skipped" >&2; slugs=""; }
  for name in "${SERVICES[@]}"; do
    { [[ -f "$(plist "$name")" ]] && loaded "$name"; } || need_install=1
  done
  for slug in $slugs; do
    for name in "${TENANT_SERVICES[@]}"; do
      { [[ -f "$(tplist "$slug" "$name")" ]] && tloaded "$slug" "$name"; } || need_install=1
    done
  done
  if [[ "$need_install" == 1 ]]; then
    cmd_install
  else
    for name in "${SERVICES[@]}"; do
      # || warn (not abort): under set -e one failed kickstart would silently
      # skip every remaining service and leave the stack partially rebooted.
      launchctl kickstart -k "gui/$UID_NUM/$(label "$name")" \
        || echo "    WARNING: kickstart $name failed — check: kb-dev status" >&2
    done
    for slug in $slugs; do
      for name in "${TENANT_SERVICES[@]}"; do
        launchctl kickstart -k "gui/$UID_NUM/$(tlabel "$slug" "$name")" \
          || echo "    WARNING: kickstart t.$slug.$name failed — check: kb-dev status" >&2
      done
    done
  fi
  echo
  bold "Stack is up (supervised by launchd; each service auto-restarts on crash):"
  echo "  • backend   → http://localhost:8000"
  echo "  • frontend  → http://localhost:8091   (public via the root cloudflared daemon, if configured)"
  echo "  • inference → http://localhost:8001   (/docs for the API)"
  echo "  • ingest    → http://localhost:8030   (signed POST /ingest/<source>; /healthz)"
  echo "  • reanalyze → (no port)"
  echo "  • resolve   → (no port)               (kb-mcp-entity-resolved; describes blank entities + merge suggestions)"
  echo "  • consolidate → (no port)             (rebuilds concepts from memories + active entities)"
  echo "  • mcp       → http://localhost:3000/mcp (MCP streamable-http tool surface)"
  echo "  • agent-gateway → http://127.0.0.1:${KB_GATEWAY_PORT:-8040} (tenant kb-agent surface)"
  echo "  • backup    → (no port)               (kb-backup run; every ${KB_BACKUP_INTERVAL:-86400}s)"
  echo "  • mcp-public → http://127.0.0.1:3100/mcp (AUTHED; origin for \$KB_MCP_PUBLIC_HOSTNAME — refuses to start without KB_MCP_PUBLIC_AUTH or KB_MCP_PUBLIC_HOSTNAME)"
  echo "  • database  → $KB_DB_CONTAINER (docker compose ps / logs)"
  echo
  echo "kb-dev status | kb-dev logs [name] | kb-dev stop"
}

cmd_stop() {
  for name in "${SERVICES[@]}"; do
    launchctl bootout "gui/$UID_NUM/$(label "$name")" 2>/dev/null \
      && bold "stopped $name" || echo "$name was not running"
  done
  for slug in $(tenant_slugs all); do
    for name in "${TENANT_SERVICES[@]}"; do
      launchctl bootout "gui/$UID_NUM/$(tlabel "$slug" "$name")" 2>/dev/null \
        && bold "stopped t.$slug.$name" || echo "t.$slug.$name was not running"
    done
  done
  echo "Database keeps running; stop it with: docker compose stop"
}

cmd_restart() {
  local targets=("$@") all=0
  [[ ${#targets[@]} -eq 0 ]] && { targets=("${SERVICES[@]}"); all=1; }
  for name in "${targets[@]}"; do
    launchctl kickstart -k "gui/$UID_NUM/$(label "$name")" && bold "restarted $name" \
      || echo "WARNING: kickstart $name failed — check: kb-dev status" >&2
  done
  # Bare `kb-dev restart` also restarts every tenant's daemon set; named restarts
  # stay owner-only.
  [[ "$all" == 1 ]] || return 0
  for slug in $(tenant_slugs all); do
    for name in "${TENANT_SERVICES[@]}"; do
      launchctl kickstart -k "gui/$UID_NUM/$(tlabel "$slug" "$name")" && bold "restarted t.$slug.$name" \
        || echo "WARNING: kickstart t.$slug.$name failed — check: kb-dev status" >&2
    done
  done
}

probe() { curl -s -o /dev/null -m 3 -w '%{http_code}' "$1" 2>/dev/null || echo "---"; }

cmd_status() {
  local state pid mcp_public_code
  for name in "${SERVICES[@]}"; do
    state="not loaded"; pid=""
    if loaded "$name"; then
      state="$(launchctl print "gui/$UID_NUM/$(label "$name")" 2>/dev/null \
               | awk -F' = ' '/^\tstate/ {print $2; exit}')"
      pid="$(launchctl print "gui/$UID_NUM/$(label "$name")" 2>/dev/null \
             | awk -F' = ' '/^\tpid/ {print $2; exit}')"
    fi
    printf '  %-10s %-12s %s\n' "$name" "${state:-?}" "${pid:+pid $pid}"
  done
  for slug in $(tenant_slugs all); do
    for name in "${TENANT_SERVICES[@]}"; do
      state="not loaded"; pid=""
      if tloaded "$slug" "$name"; then
        state="$(launchctl print "gui/$UID_NUM/$(tlabel "$slug" "$name")" 2>/dev/null \
                 | awk -F' = ' '/^\tstate/ {print $2; exit}')"
        pid="$(launchctl print "gui/$UID_NUM/$(tlabel "$slug" "$name")" 2>/dev/null \
               | awk -F' = ' '/^\tpid/ {print $2; exit}')"
      fi
      printf '  %-10s %-12s %s\n' "t.$slug.$name" "${state:-?}" "${pid:+pid $pid}"
    done
  done
  echo
  # /healthz is the API's one unauthenticated route (/docs is off, and with
  # KB_DASHBOARD_AUTH=cf-access every /api route answers 401 to a bare probe).
  echo "  backend   HTTP $(probe http://localhost:8000/healthz)"
  echo "  frontend  HTTP $(probe http://localhost:8091/)"
  echo "  inference HTTP $(probe http://localhost:8001/health)"
  echo "  ingest    HTTP $(probe http://localhost:8030/healthz)"
  # A bare GET isn't a valid MCP handshake, so a 4xx here still means the server is
  # up and speaking HTTP — only '---' means down.
  echo "  mcp       HTTP $(probe http://localhost:3000/mcp)"
  # For the public listener a 401 IS the healthy reading — up and correctly
  # refusing an unauthenticated bare GET. Only '---' (or a 5xx) means trouble.
  mcp_public_code="$(probe http://127.0.0.1:3100/mcp)"
  echo "  mcp-public HTTP $mcp_public_code$([[ "$mcp_public_code" == 401 ]] && echo ' (OK: authed)')"
  echo "  ollama    HTTP $(probe "$OLLAMA_BASE_URL/api/tags")"
  echo "  database  $(docker inspect -f '{{.State.Health.Status}}' "$KB_DB_CONTAINER" 2>/dev/null || echo down)"
}

cmd_logs() {
  local name="${1:-}"
  if [[ -n "$name" ]]; then
    exec tail -n 50 -F "$LOG_DIR/$name.log"
  fi
  exec tail -n 20 -F "$LOG_DIR"/*.log
}

cmd_uninstall() {
  for name in "${SERVICES[@]}"; do
    launchctl bootout "gui/$UID_NUM/$(label "$name")" 2>/dev/null || true
    rm -f "$(plist "$name")"
    bold "removed $(label "$name")"
  done
  for slug in $(tenant_slugs all); do
    for name in "${TENANT_SERVICES[@]}"; do
      launchctl bootout "gui/$UID_NUM/$(tlabel "$slug" "$name")" 2>/dev/null || true
      rm -f "$(tplist "$slug" "$name")"
      bold "removed $(tlabel "$slug" "$name")"
    done
  done
}

case "${1:-start}" in
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   shift; cmd_restart "$@" ;;
  status)    cmd_status ;;
  logs)      shift || true; cmd_logs "${1:-}" ;;
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  *) echo "usage: kb-dev [start|stop|restart|status|logs|install|uninstall]" >&2; exit 64 ;;
esac
