#!/usr/bin/env bash
# Expose the AUTHENTICATED MCP listener (kb-svc.sh `mcp-public`, default port 3100)
# over HTTPS through a Cloudflare tunnel.
#
# This script never targets the plain `mcp` service (default port 3000): that
# listener has no auth wall, and anything a tunnel forwards to it is the whole tool
# surface, open to the internet. Before touching cloudflared it checks that:
#   - the target port is not the plain mcp port,
#   - KB_Core/.env configures mcp-public (KB_MCP_PUBLIC_AUTH + KB_MCP_PUBLIC_HOSTNAME),
#   - CF_HOSTNAME matches KB_MCP_PUBLIC_HOSTNAME (otherwise every request is 421),
#   - if something already listens on the port, it rejects an unauthenticated request.
# Set up auth first: see docs/exposure.md ("Public MCP server").
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.tunnel"
KB_CORE_ENV="$SCRIPT_DIR/../KB_Core/.env"
CF_DIR="$SCRIPT_DIR/.cloudflare"
CREDS_FILE="$CF_DIR/credentials.json"
CONFIG_FILE="$CF_DIR/tunnel.yml"

die() { echo "ERROR: $*" >&2; exit 1; }

# Last assignment of KEY in a dotenv file, quotes stripped. Reads only the one key:
# the file is never sourced, so its secrets never reach cloudflared's environment.
env_value() {
  local key="$1" file="$2"
  [[ -f "$file" ]] || return 0
  sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}=//p" "$file" | tail -n 1 \
    | sed -E "s/[[:space:]]+#.*$//; s/^[\"']//; s/[\"'][[:space:]]*$//; s/[[:space:]]+$//"
}

# ── Load env ──────────────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  die "$ENV_FILE not found. Copy .env.tunnel.example and fill it in."
fi
# shellcheck source=/dev/null
source "$ENV_FILE"

CF_TUNNEL_NAME="${CF_TUNNEL_NAME:-my-kb-tunnel}"
: "${CF_HOSTNAME:?set CF_HOSTNAME in .env.tunnel}"

# ── Refuse anything but the authenticated listener ────────────────────────────
if [[ -n "${MCP_PORT:-}" ]]; then
  die "MCP_PORT is no longer read: it used to point the tunnel at the plain,
       UNAUTHENTICATED mcp listener. Replace it with MCP_PUBLIC_PORT (the mcp-public
       port, default 3100) in $ENV_FILE."
fi
PLAIN_MCP_PORT="$(env_value KB_MCP_PORT "$KB_CORE_ENV")"
PLAIN_MCP_PORT="${PLAIN_MCP_PORT:-3000}"
DEFAULT_PUBLIC_PORT="$(env_value KB_MCP_PUBLIC_PORT "$KB_CORE_ENV")"
MCP_PUBLIC_PORT="${MCP_PUBLIC_PORT:-${DEFAULT_PUBLIC_PORT:-3100}}"
[[ "$MCP_PUBLIC_PORT" =~ ^[0-9]+$ ]] || die "MCP_PUBLIC_PORT must be a port number"
if [[ "$MCP_PUBLIC_PORT" == "3000" || "$MCP_PUBLIC_PORT" == "$PLAIN_MCP_PORT" ]]; then
  die "refusing to tunnel port $MCP_PUBLIC_PORT: that is the plain mcp listener, which
       has NO auth. Point MCP_PUBLIC_PORT at the mcp-public listener (default 3100)."
fi

PUBLIC_AUTH="$(env_value KB_MCP_PUBLIC_AUTH "$KB_CORE_ENV")"
PUBLIC_HOSTNAME="$(env_value KB_MCP_PUBLIC_HOSTNAME "$KB_CORE_ENV")"
if [[ -z "$PUBLIC_AUTH" ]]; then
  die "KB_MCP_PUBLIC_AUTH is not set in $KB_CORE_ENV: mcp-public has no auth mode,
       so there is nothing safe to tunnel to. Set KB_MCP_PUBLIC_AUTH=cf-access (or
       internal-secret) and its settings first; see KB_Core/.env.example."
fi
if [[ -z "$PUBLIC_HOSTNAME" ]]; then
  die "KB_MCP_PUBLIC_HOSTNAME is not set in $KB_CORE_ENV. Set it to CF_HOSTNAME."
fi
if [[ "$PUBLIC_HOSTNAME" != "$CF_HOSTNAME" ]]; then
  die "CF_HOSTNAME ($CF_HOSTNAME) differs from KB_MCP_PUBLIC_HOSTNAME
       ($PUBLIC_HOSTNAME): mcp-public would answer every tunnelled request 421."
fi

# If something already listens on the port, it must be the auth wall: an
# unauthenticated request has to come back 401. Not listening yet is fine (start
# mcp-public whenever); anything else answering is refused.
if command -v curl &>/dev/null; then
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
    "http://127.0.0.1:${MCP_PUBLIC_PORT}/mcp" || true)"
  if [[ -n "$status" && "$status" != "000" && "$status" != "401" ]]; then
    die "port $MCP_PUBLIC_PORT answered an unauthenticated request with HTTP $status
       (expected 401): it is not the authenticated mcp-public listener."
  fi
  if [[ -z "$status" || "$status" == "000" ]]; then
    echo "note: nothing answers on port $MCP_PUBLIC_PORT yet; start mcp-public (kb-dev.sh)."
  fi
fi

# ── Ensure cloudflared is installed ───────────────────────────────────────────
if ! command -v cloudflared &>/dev/null; then
  if command -v brew &>/dev/null; then
    echo "Installing cloudflared via Homebrew..."
    brew install cloudflared
  else
    die "cloudflared is not installed. Install it (see Cloudflare's cloudflared downloads page), then re-run."
  fi
fi

# ── Login (no-op if already authenticated) ────────────────────────────────────
cloudflared tunnel login 2>/dev/null || true

# ── Create tunnel if it doesn't exist yet ─────────────────────────────────────
tunnel_id() {
  cloudflared tunnel list --output json 2>/dev/null | CF_TUNNEL_NAME="$CF_TUNNEL_NAME" python3 -c \
    "import json,os,sys; t=[x for x in json.load(sys.stdin) if x['name']==os.environ['CF_TUNNEL_NAME']]; print(t[0]['id'] if t else '')" \
    || true
}
TUNNEL_ID="$(tunnel_id)"

if [[ -z "$TUNNEL_ID" ]]; then
  echo "Creating tunnel '$CF_TUNNEL_NAME'..."
  cloudflared tunnel create "$CF_TUNNEL_NAME"
  TUNNEL_ID="$(tunnel_id)"
  [[ -n "$TUNNEL_ID" ]] || die "could not find tunnel '$CF_TUNNEL_NAME' after creating it"
  echo "Tunnel ID: $TUNNEL_ID"
fi

# Copy credentials to repo-local dir so config stays self-contained
mkdir -p "$CF_DIR"
SRC_CREDS="$HOME/.cloudflared/${TUNNEL_ID}.json"
if [[ -f "$SRC_CREDS" && ! -f "$CREDS_FILE" ]]; then
  cp "$SRC_CREDS" "$CREDS_FILE"
fi

# ── Write resolved tunnel.yml ─────────────────────────────────────────────────
cat > "$CONFIG_FILE" <<YAML
tunnel: ${TUNNEL_ID}
credentials-file: ${CREDS_FILE}

ingress:
  - hostname: ${CF_HOSTNAME}
    service: http://localhost:${MCP_PUBLIC_PORT}
  - service: http_status:404
YAML

# ── Route DNS (idempotent) ────────────────────────────────────────────────────
cloudflared tunnel route dns "$CF_TUNNEL_NAME" "$CF_HOSTNAME" 2>/dev/null || true

# ── Run ───────────────────────────────────────────────────────────────────────
echo ""
echo "Tunnel '$CF_TUNNEL_NAME' → https://${CF_HOSTNAME}"
echo "Proxying → http://localhost:${MCP_PUBLIC_PORT} (authenticated mcp-public)"
echo ""
exec cloudflared tunnel --config "$CONFIG_FILE" run
