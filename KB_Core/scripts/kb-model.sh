#!/usr/bin/env bash
# kb-model — show or swap the local chat model KB_Inference serves.
#
#   kb-model.sh                        status: configured / served / resident
#   kb-model.sh list                   chat models available locally
#   kb-model.sh use TAG                swap to TAG
#   kb-model.sh use TAG --warm         swap, then load it before real traffic arrives
#   kb-model.sh use TAG --think off    swap and disable chain-of-thought
#   kb-model.sh use TAG --think default  hand the decision back to the model
#
# WHERE THE CONFIG LIVES
# The model is `OLLAMA_MODEL` in KB_Core/.env, which kb-svc.sh sources (`set -a`)
# into every service's environment before launching it. That file is gitignored, so
# a swap is a local config change: it touches no tracked file, needs no rebuild, and
# survives branch switches. With OLLAMA_MODEL unset the server falls back to
# conversation.py's DEFAULT_MODEL. `--think` writes `OLLAMA_THINK` the same way.
#
# THINKING IS A DECODING SETTING, NOT A MODEL
# `--think off` is not cosmetic: measured, one extract_entities call runs
# 100-175s with thinking on and ~7s with it off, and the thinking tokens are what
# exhaust num_ctx in the runaway cases. `off` and `default` are DIFFERENT states —
# `default` removes the variable so the model decides (gemma4 thinks), `off` disables
# it explicitly. Changing only `--think` still restarts the service, because the
# setting is read at process start.
#
# WHY THE UNLOAD IS PART OF THE SWAP
# Ollama pins each model with keep_alive=-1 (see OLLAMA_KEEP_ALIVE). Two chat models
# pinned at once can overrun the GPU's wired-memory limit, and Ollama
# starts evicting and reloading between calls. That starves whichever model is actually
# serving and makes latency unmeasurable: a lighter setting can time slower than a
# heavier one purely from that contention. So `use` stops the outgoing model before
# restarting the service, rather than leaving both resident and hoping.
#
# Only the inference agent is restarted. The rest of the fleet keeps running.

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
ENV_FILE="$KB_REPO/.env"

# Load KB_Core/.env so KB_SERVICE_PREFIX (and anything else it sets) is available
# for the launchd label below, the same way kb-dev.sh does.
if [[ -f "$ENV_FILE" ]]; then set -a; source "$ENV_FILE"; set +a; fi

PORT="${KB_INFERENCE_PORT:-8001}"
HEALTH="http://127.0.0.1:${PORT}/health"
AGENT="${KB_SERVICE_PREFIX:-com.kb}.inference"
WAIT_SECS="${KB_MODEL_WAIT:-180}"

die()  { printf 'kb-model: %s\n' "$*" >&2; exit 1; }

# curl to KB_Inference. With KB_INFERENCE_TOKEN set (KB_Core/.env) the server rejects any
# request without it; the header goes in through a process-substitution file so the token
# never appears in `ps`.
icurl() {
  if [[ -n "${KB_INFERENCE_TOKEN:-}" ]]; then
    curl -H @<(printf 'Authorization: Bearer %s\n' "$KB_INFERENCE_TOKEN") "$@"
  else
    curl "$@"
  fi
}
info() { printf '%s\n' "$*"; }

# The embedding model is managed separately (OLLAMA_EMBED_MODEL) and must never be
# swapped or unloaded by this tool — changing it would invalidate every stored vector.
embed_model() {
  awk -F= '/^[[:space:]]*OLLAMA_EMBED_MODEL=/ {v=$2} END {print v}' "$ENV_FILE" 2>/dev/null \
    | tr -d '"'\''[:space:]'
}

configured() {
  [[ -f $ENV_FILE ]] || { echo ""; return; }
  # Last assignment wins, matching how `source` would resolve duplicates.
  awk -F= '/^[[:space:]]*OLLAMA_MODEL=/ {v=$2} END {print v}' "$ENV_FILE" \
    | tr -d '"'\''[:space:]'
}

served() {
  icurl -fsS -m 5 "$HEALTH" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("model",""))' 2>/dev/null || true
}

# on | off | default | absent. "absent" means the running server has no `think` field at all
# — it predates it — which is NOT the same as "the model decides" and must not be reported
# as agreement. A server started before this change serves whatever it was built with.
served_think() {
  icurl -fsS -m 5 "$HEALTH" 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
if "think" not in d:
    print("absent")
else:
    v = d["think"]
    print("default" if v is None else ("on" if v else "off"))
' 2>/dev/null || true
}

have_locally() { ollama list | awk 'NR>1 {print $1}' | grep -qx "$1"; }

configured_think() {
  [[ -f $ENV_FILE ]] || { echo ""; return; }
  awk -F= '/^[[:space:]]*OLLAMA_THINK=/ {v=$2} END {print v}' "$ENV_FILE" \
    | tr -d '"'\''[:space:]'
}

# Write KEY=VALUE pairs into .env, preserving everything else. .env holds credentials, so
# keep its mode and leave ONE timestamped backup for the whole write — a backup per key
# would bury the pre-change state under near-identical files.
#
# An empty VALUE DELETES the key, which is how `--think default` hands the decision back to
# the model. Setting OLLAMA_THINK=false instead would explicitly disable thinking, which is
# a different state (see conversation.py's _parse_think).
set_env_vars() {
  local tmp cur mode stamp key val comment
  tmp=$(mktemp)
  if [[ -f $ENV_FILE ]]; then
    stamp=$(date +%Y%m%dT%H%M%S)
    cp -p "$ENV_FILE" "${ENV_FILE}.bak.${stamp}"
    mode=$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || echo 600)
    cat "$ENV_FILE" >"$tmp"
  else
    mode=600
    : >"$tmp"
  fi

  while (( $# >= 2 )); do
    key=$1; val=$2; shift 2
    cur=$(mktemp)
    if [[ -z $val ]]; then
      grep -v "^[[:space:]]*${key}=" "$tmp" >"$cur" || true
    elif grep -q "^[[:space:]]*${key}=" "$tmp"; then
      awk -v k="$key" -v v="$val" '
        $0 ~ "^[[:space:]]*" k "=" && !seen { print k "=" v; seen=1; next }
        { print }
      ' "$tmp" >"$cur"
    else
      case $key in
        OLLAMA_MODEL) comment="# Local chat model KB_Inference serves. Swap with: kb-model.sh use TAG" ;;
        OLLAMA_THINK) comment="# Chain-of-thought on/off. Unset = the model decides. kb-model.sh use TAG --think off" ;;
        *)            comment="" ;;
      esac
      cat "$tmp" >"$cur"
      # Only if it isn't already there. Deleting a key (empty VALUE) leaves its comment
      # behind, so a benchmark that toggles --think across arms would otherwise append a
      # duplicate comment on every cycle.
      if [[ -n $comment ]] && ! grep -qF "$comment" "$cur"; then
        printf '\n%s\n' "$comment" >>"$cur"
      fi
      printf '%s=%s\n' "$key" "$val" >>"$cur"
    fi
    mv "$cur" "$tmp"
  done

  chmod "$mode" "$tmp"
  mv "$tmp" "$ENV_FILE"
}

cmd_status() {
  local cfg srv cfg_think srv_think
  cfg=$(configured); srv=$(served)
  cfg_think=$(configured_think); srv_think=$(served_think)
  info "configured (KB_Core/.env): ${cfg:-<unset — server falls back to conversation.py DEFAULT_MODEL>}"
  info "serving   (:${PORT}/health): ${srv:-<unreachable>}"
  info "think     configured=${cfg_think:-<unset — model decides>} serving=${srv_think:-<unreachable>}"
  if [[ -n $cfg && -n $srv && $cfg != "$srv" ]]; then
    info ""
    info "MISMATCH: .env says '${cfg}' but the running server reports '${srv}'."
    info "The service has not been restarted since the config changed — run: $0 use ${cfg}"
  fi
  info ""
  info "resident in Ollama:"
  ollama ps
}

cmd_list() {
  local emb; emb=$(embed_model)
  info "locally available (the embedding model is excluded — it is not swappable here):"
  ollama list | awk -v emb="${emb:-qwen3-embedding:0.6b}" 'NR==1 || $1 != emb'
}

cmd_use() {
  local tag=${1:-} warm="" think_arg="" think_val=""
  [[ -n $tag ]] || die "usage: $0 use TAG [--warm] [--think on|off|default]"
  shift || true

  while (( $# )); do
    case $1 in
      --warm)  warm="--warm"; shift ;;
      --think)
        think_arg=${2:-}
        case $think_arg in
          on)      think_val="true";  shift 2 ;;
          off)     think_val="false"; shift 2 ;;
          default) think_val="";      shift 2 ;;
          *) die "--think takes on | off | default (got '${think_arg:-<nothing>}')" ;;
        esac ;;
      *) die "unknown option '$1' (try: --warm, --think on|off|default)" ;;
    esac
  done

  local emb; emb=$(embed_model)
  [[ $tag != "${emb:-qwen3-embedding:0.6b}" ]] || \
    die "'$tag' is the embedding model. Swapping it would invalidate every stored vector."

  have_locally "$tag" || die "'$tag' is not pulled. Run: ollama pull $tag"

  local old srv old_think srv_think
  old=$(configured); srv=$(served); old_think=$(configured_think); srv_think=$(served_think)
  local outgoing="${srv:-$old}"

  # A no-op check must consider BOTH knobs. Comparing only the model would skip the restart
  # that a --think change needs, leaving the service running the previous decoding settings
  # while .env claims otherwise — the exact mismatch cmd_status exists to catch.
  if [[ $tag == "$old" && $tag == "$srv" && -z $think_arg ]]; then
    info "Already configured and serving '$tag'. Nothing to do."
    return 0
  fi
  # The RUNNING process must agree too, not just .env. Comparing .env against .env alone
  # would report "nothing to do" when something changed OLLAMA_THINK without a restart,
  # while the live server still serves the OLD setting (e.g. .env unset, server still on
  # think=true), which is exactly the drift cmd_status exists to surface. Config equality is not proof of deployed state.
  if [[ $tag == "$old" && $tag == "$srv" && $think_val == "$old_think" \
        && $srv_think == "$think_arg" ]]; then
    info "Already configured and serving '$tag' with think=${think_arg}. Nothing to do."
    return 0
  fi

  info "swapping ${outgoing:-<none>} -> ${tag}"
  [[ -n $think_arg ]] && info "  think: ${old_think:-<model default>} -> ${think_arg}"

  if [[ -n $think_arg ]]; then
    set_env_vars OLLAMA_MODEL "$tag" OLLAMA_THINK "$think_val"
  else
    set_env_vars OLLAMA_MODEL "$tag"
  fi
  info "  .env updated (backup alongside it)"

  # Unload the outgoing model FIRST so its memory is free before the new one loads.
  # Never unload the embedder: other services depend on it and it is cheap to keep.
  if [[ -n $outgoing && $outgoing != "$tag" && $outgoing != "${emb:-qwen3-embedding:0.6b}" ]]; then
    if ollama ps | awk 'NR>1 {print $1}' | grep -qx "$outgoing"; then
      ollama stop "$outgoing" >/dev/null 2>&1 || true
      info "  unloaded ${outgoing}"
    fi
  fi

  launchctl kickstart -k "gui/$(id -u)/${AGENT}" >/dev/null 2>&1 \
    || die "could not restart ${AGENT} — is it loaded? (launchctl list | grep ${AGENT})"
  info "  restarted ${AGENT}"

  local waited=0
  while (( waited < WAIT_SECS )); do
    sleep 3; waited=$((waited + 3))
    [[ $(served) == "$tag" ]] && break
  done

  local now; now=$(served)
  if [[ $now != "$tag" ]]; then
    die "service came back reporting '${now:-<unreachable>}', not '${tag}', after ${WAIT_SECS}s.
     .env has been updated; check the service log:
       tail -40 ~/Library/Logs/kb/inference.log"
  fi
  info "  serving ${tag} (verified via ${HEALTH})"

  if [[ -n $think_arg ]]; then
    local now_think; now_think=$(served_think)
    case $now_think in
      absent)
        info "  WARNING: /health exposes no 'think' field — this server build predates it."
        info "           .env is set and WILL apply on the next start, but this tool cannot"
        info "           confirm the running process picked it up. Do not trust a benchmark"
        info "           arm recorded against it." ;;
      "$think_arg")
        info "  think=${think_arg} (verified via ${HEALTH})" ;;
      *)
        die "service reports think='${now_think:-<unreachable>}', expected '${think_arg}'.
     .env has been updated; check the service log:
       tail -40 ~/Library/Logs/kb/inference.log" ;;
    esac
  fi

  if [[ $warm == "--warm" ]]; then
    info "  warming (first load pulls ~GB into GPU memory; this is the cold-start cost)"
    icurl -fsS -m 600 -X POST "http://127.0.0.1:${PORT}/chat" \
      -H 'Content-Type: application/json' \
      -d '{"session":"kb-model-warm","message":"ok"}' >/dev/null \
      && icurl -fsS -m 10 -X DELETE "http://127.0.0.1:${PORT}/sessions/kb-model-warm?purge=true" >/dev/null \
      && info "  warm" || info "  warm call failed — the model still loads on first real request"
  fi

  info ""
  ollama ps
}

case "${1:-status}" in
  status|"") cmd_status ;;
  list)      cmd_list ;;
  use)       shift; cmd_use "$@" ;;
  -h|--help|help)
    sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$1' (try: status | list | use TAG [--warm])" ;;
esac
