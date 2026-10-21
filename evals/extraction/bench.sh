#!/usr/bin/env bash
# bench.sh — run the frozen extraction corpus through each ARM in turn.
#
# An ARM is a (model, thinking) pair, written MODEL/THINK:
#
#   ./bench.sh                                    # 12b vs 26b, thinking on for both
#   ./bench.sh gemma4:26b/on                      # one arm only
#   BENCH_SUBSET=20 ./bench.sh                    # seeded RANDOM 20 per set
#   BENCH_LIMIT=2 ./bench.sh                      # FIRST 2 per set — smoke only, skewed
#
# THINKING IS HELD ON, NOT VARIED
# A deliberate design decision: thinking is required for extraction quality, so it is a fixed
# condition rather than an axis. The arms still carry `/on` explicitly and the setting is
# verified through /health before each arm runs — a benchmark that ASSUMED thinking was on
# would silently compare two arms under whatever the service happened to be left in.
#
# The MODEL/THINK syntax is retained because setting it explicitly is what makes the
# verification possible, and because a thinking ablation may be worth running later against
# a separate question (see the runaway note below). It is not part of this comparison.
#
# Arms are ordered so all arms of one MODEL run together. Switching thinking only restarts
# KB_Inference (cheap); switching model additionally evicts and reloads GB of weights.
#
# Use BENCH_SUBSET, not BENCH_LIMIT, for anything you intend to read. The corpus files are
# sorted by doc_kind, so a head slice is dominated by one or two kinds. BENCH_SUBSET draws a
# seeded random sample instead, and the seed is fixed across arms so every arm sees the same
# items.
#
# OUTPUT LAYOUT — everything for one benchmark lands under one directory:
#
#   evals/extraction/runs/<UTC stamp>/
#     bench.log                     orchestration: what ran, when, swap events, failures
#     meta.json                     arms, corpus checksums, host settings, start/end
#     <model-slug>-think-<on|off>/
#       items.jsonl                 ONE LINE PER ITEM, written as it completes
#       result.json                 assembled at the end
#       run.log                     that arm's runner stdout
#     judgments.jsonl               written later by judge.py
#     scores.json                   written later by judge.py
#
# items.jsonl is the important one: the run is checkpointed per item and resumable. Killing
# this script keeps every item already finished — re-running with the same --run-dir continues.
#
# TWO GUARDS
# 1. It refuses to start while another run.py is alive. Two runners against one Ollama
#    starve each other into timeouts: the GPU is serialized, every call blows its timeout,
#    and both runs record nothing but empty replies.
# 2. It refuses to start while the reanalysis daemon is loaded, for the same reason.
#    Contention does not merely distort latency — it makes the whole run garbage. Override
#    with BENCH_ALLOW_CONTENTION=1 only if you intend to read hygiene metrics and discard
#    every timing.
#
# RUNAWAY NOTE — expect a long tail, and expect some of it to be wasted
# Production runs uncapped (OLLAMA_NUM_PREDICT=0). Uncapped calls can run many minutes,
# return tens of thousands of chars, and still come back `truncated=True` — uncapped,
# "length" means the context window ran out, not the output cap. Those calls produce no parseable JSON, so the minutes buy nothing.
#
# That is a capacity problem (num_ctx), NOT an argument about thinking, and it affects both
# arms equally, so it does not bias this comparison. It does mean BENCH_TIMEOUT must be large
# enough to let a slow-but-finishing call complete, while still cutting off the ones that
# never will. Whatever fraction of items truncate is itself a result worth reading.

set -uo pipefail          # deliberately NOT -e: one arm failing must not abort the rest

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# Pull just KB_SERVICE_PREFIX out of KB_Core/.env, if present, so guard 2 below checks
# the actual launchd label prefix rather than a hardcoded default. This does NOT
# source the file (bench.sh has no business picking up arbitrary env from it) — the env
# var still wins if already set.
KB_SERVICE_PREFIX="${KB_SERVICE_PREFIX:-$(grep -E '^KB_SERVICE_PREFIX=' "$REPO/KB_Core/.env" 2>/dev/null | tail -1 | cut -d= -f2-)}"
# Same for KB_INFERENCE_TOKEN: a server started with one rejects every untokened request.
# Exported so run.py (and kb-model.sh) send it too.
KB_INFERENCE_TOKEN="${KB_INFERENCE_TOKEN:-$(grep -E '^KB_INFERENCE_TOKEN=' "$REPO/KB_Core/.env" 2>/dev/null | tail -1 | cut -d= -f2-)}"
export KB_INFERENCE_TOKEN
icurl() {   # header via process substitution, so the token never shows in `ps`
  if [[ -n "$KB_INFERENCE_TOKEN" ]]; then
    curl -H @<(printf 'Authorization: Bearer %s\n' "$KB_INFERENCE_TOKEN") "$@"
  else
    curl "$@"
  fi
}

PY="${BENCH_PY:-$REPO/KB_Core/.venv/bin/python}"
DATA_DIR="${KB_EVAL_DATA_DIR:+$KB_EVAL_DATA_DIR/extraction}"
DATA_DIR="${DATA_DIR:-$HERE}"
KB_MODEL="$REPO/KB_Core/scripts/kb-model.sh"
URL="${BENCH_URL:-http://localhost:8001}"
LIMIT="${BENCH_LIMIT:-0}"
SUBSET="${BENCH_SUBSET:-0}"
SUBSET_SEED="${BENCH_SUBSET_SEED:-0}"
TIMEOUT="${BENCH_TIMEOUT:-300}"

# The comparison: two models, thinking held on for both.
ARMS=("$@")
[[ ${#ARMS[@]} -gt 0 ]] || ARMS=(gemma4:12b-it-q8_0/on gemma4:26b/on)

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUNS="$HERE/runs/$STAMP"
mkdir -p "$RUNS"
BENCH_LOG="$RUNS/bench.log"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%SZ)" "$*" | tee -a "$BENCH_LOG"; }

[[ -x $PY ]] || { log "FATAL: interpreter not found: $PY"; exit 1; }
[[ -x $KB_MODEL ]] || { log "FATAL: kb-model.sh not found: $KB_MODEL"; exit 1; }
export KB_ENRICHMENT_CONFIG="${KB_ENRICHMENT_CONFIG:-$REPO/KB_Core/enrichment.toml}"
[[ -f $KB_ENRICHMENT_CONFIG ]] || { log "FATAL: no enrichment.toml at $KB_ENRICHMENT_CONFIG"; exit 1; }

# --- guard 1: no other runner ------------------------------------------------------
if pgrep -f "extraction/run\.py" >/dev/null 2>&1; then
  log "FATAL: another extraction run.py is already running."
  log "  Two runners against one Ollama starve each other into total timeout."
  log "  Inspect with:  ps aux | grep 'extraction/run.py'"
  exit 1
fi

# --- guard 2: no background enrichment traffic -------------------------------------
if launchctl list 2>/dev/null | grep -q "${KB_SERVICE_PREFIX:-com.kb}.reanalyze"; then
  if [[ ${BENCH_ALLOW_CONTENTION:-0} != 1 ]]; then
    log "FATAL: the reanalysis daemon is loaded and will contend for the same Ollama."
    log "  A past run timed out on every call this way and recorded only empty replies."
    log "  Stop it first:   kb-dev stop reanalyze"
    log "  Or accept it:    BENCH_ALLOW_CONTENTION=1 $0 $*   (timings become meaningless)"
    exit 1
  fi
  log "WARNING: reanalysis daemon is running and BENCH_ALLOW_CONTENTION=1 — timings are junk."
  daemon_note="running (contended; latency invalid)"
else
  daemon_note="not loaded"
fi

# --- preflight: the server must be able to report its thinking setting --------------
# Without this field an arm cannot be verified, and a 2x2 whose arms are indistinguishable
# is worse than no benchmark. The field landed with the OLLAMA_THINK work; a server started
# before that will not have it and must be restarted.
think_field="$(icurl -fsS -m 10 "$URL/health" 2>/dev/null \
  | python3 -c 'import json,sys; print("yes" if "think" in json.load(sys.stdin) else "no")' \
  2>/dev/null)"
if [[ $think_field != yes ]]; then
  log "FATAL: ${URL}/health exposes no 'think' field (got: ${think_field:-unreachable})."
  log "  This server build predates it, so no arm's thinking setting can be verified."
  log "  Restart the inference service, then re-run."
  exit 1
fi

log "benchmark $STAMP"
log "  arms:     ${ARMS[*]}"
if [[ ${SUBSET:-0} -gt 0 ]]; then
  selection="random subset ${SUBSET}/set (seed ${SUBSET_SEED})"
elif [[ ${LIMIT:-0} -gt 0 ]]; then
  selection="HEAD ${LIMIT}/set — skewed, smoke only"
else
  selection="full corpus"
fi
log "  corpus:   $(wc -l <"$DATA_DIR/gold.jsonl" | tr -d ' ') gold + $(wc -l <"$DATA_DIR/sample.jsonl" | tr -d ' ') sample"
log "  selection: $selection"
log "  prompts:  $KB_ENRICHMENT_CONFIG"
log "  reanalyze daemon: $daemon_note"
log "  output:   $RUNS"

python3 - "$RUNS/meta.json" "$STAMP" "$daemon_note" "$KB_ENRICHMENT_CONFIG" "$DATA_DIR" \
         "$LIMIT" "$SUBSET" "$SUBSET_SEED" "${ARMS[@]}" <<'PY'
import hashlib, json, sys, pathlib
out, stamp, daemon, cfg, data_dir, limit, subset, seed, *arms = sys.argv[1:]
here = pathlib.Path(data_dir)
sha = lambda p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()[:12]
# selection: what this run actually measured. Without it a results folder cannot be
# reproduced or compared to another — the single most important field here.
pathlib.Path(out).write_text(json.dumps({
    "started_at": stamp,
    "arms": [{"model": a.rsplit("/", 1)[0], "think": a.rsplit("/", 1)[1]} for a in arms],
    "selection": ({"mode": "subset", "n_per_set": int(subset), "seed": int(seed)}
                  if int(subset) else
                  {"mode": "head", "n_per_set": int(limit)} if int(limit) else
                  {"mode": "full corpus"}),
    "enrichment_config": cfg, "reanalyze_daemon": daemon,
    "gold_sha": sha(here / "gold.jsonl"), "sample_sha": sha(here / "sample.jsonl"),
}, indent=2))
PY

for arm in "${ARMS[@]}"; do
  model="${arm%/*}"; think="${arm##*/}"
  case $think in
    on|off|default) ;;
    *) log "SKIP '$arm': think must be on|off|default (got '$think')"; continue ;;
  esac

  slug="${model//:/-}"; slug="${slug//\//-}-think-${think}"
  dir="$RUNS/$slug"
  mkdir -p "$dir"

  log "--- $model (think=$think) ---"
  log "  swapping..."
  if ! "$KB_MODEL" use "$model" --think "$think" --warm >>"$BENCH_LOG" 2>&1; then
    log "  SWAP FAILED for $arm — skipping (see bench.log)"
    continue
  fi

  # Re-verify independently of kb-model.sh: both knobs, from one /health read, so an arm
  # can never be recorded against settings it did not actually run under.
  read -r serving serving_think <<<"$(icurl -fsS -m 10 "$URL/health" 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
v = d.get("think", "absent")
print(d.get("model", ""), "default" if v is None else ("absent" if v == "absent" else ("on" if v else "off")))
' 2>/dev/null)"
  if [[ $serving != "$model" || $serving_think != "$think" ]]; then
    log "  ABORT $arm: /health reports model='${serving:-unreachable}' think='${serving_think:-?}'"
    continue
  fi
  log "  serving $serving think=$serving_think — starting run (checkpointing to $slug/items.jsonl)"

  start=$(date +%s)
  "$PY" -u "$HERE/run.py" --model "$model" --url "$URL" --run-dir "$dir" \
        --timeout "$TIMEOUT" \
        ${LIMIT:+--limit "$LIMIT"} \
        ${SUBSET:+--subset "$SUBSET" --subset-seed "$SUBSET_SEED"} >"$dir/run.log" 2>&1
  rc=$?
  elapsed=$(( $(date +%s) - start ))

  n=$( [[ -f $dir/items.jsonl ]] && wc -l <"$dir/items.jsonl" | tr -d ' ' || echo 0 )
  # An arm that produced only empty replies is a failed arm even with rc=0 — that is exactly
  # what contention looks like from here, and it is easy to miss.
  empty=$( [[ -f $dir/items.jsonl ]] && grep -c '"reply_chars": 0' "$dir/items.jsonl" || echo 0 )
  if [[ $rc -eq 0 ]]; then
    log "  done: $n items in ${elapsed}s -> $slug/result.json"
  else
    log "  RUN EXITED $rc after ${elapsed}s with $n item(s) checkpointed (resumable)"
  fi
  if [[ $n -gt 0 && $empty -eq $n ]]; then
    log "  WARNING: all $n replies were EMPTY — this arm produced no usable data."
  elif [[ $empty -gt 0 ]]; then
    log "  note: $empty of $n replies were empty"
  fi
done

log "benchmark complete -> $RUNS"
log "next: judge.py --run-dir $RUNS   (pooled Claude adjudication -> scores.json)"
