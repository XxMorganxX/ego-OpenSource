#!/usr/bin/env python3
"""Replay the frozen extraction corpus through one model and record what comes out.

Prompts are read from the LIVE enrichment.toml, not copied here — a prompt edit is picked
up automatically. Memories and their prompt context come from the FROZEN JSONL, so the
only thing that varies between runs is the model.

Scoring lives in metrics.py; this file only collects. Nothing writes to the graph.

Usage: run.py --model gemma4:26b [--url http://localhost:8001] [--gold gold.jsonl]
              [--sample sample.jsonl] [--limit N] [--out results/]
Run with KB_Core's venv python (needs kb_core for load_passes/render_template).
"""
import argparse
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kb_core.ingest.inference import inference_auth_headers
from kb_core.ingest.llm import load_passes, render_template

HERE = Path(__file__).parent


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def extract_pass():
    """The production extract_entities pass, straight from enrichment.toml."""
    for p in load_passes():
        if p.name == "extract_entities":
            return p
    raise SystemExit("extract_entities pass not found — is KB_ENRICHMENT_CONFIG set?")


def call(url: str, system: str, user: str, schema: dict,
         timeout: float) -> tuple[str, int, bool]:
    """One stateless /chat turn. Returns (reply_text, latency_ms, truncated).

    `truncated` is the server's own done_reason check, not an inference from length. A
    capped reply and a malformed one are different failures with different fixes, and
    only this flag separates them.
    """
    session = f"kb-eval-{uuid.uuid4()}"
    body = json.dumps({"session": session, "system_prompt": system,
                       "message": user, "format": schema}).encode()
    req = urllib.request.Request(f"{url}/chat", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **inference_auth_headers()})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
            reply, truncated = payload["reply"], bool(payload.get("truncated"))
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as exc:
        reply, truncated = "", False
        print(f"  call failed: {exc}")
    latency_ms = int((time.perf_counter() - t0) * 1000)
    try:
        purge = urllib.request.Request(f"{url}/sessions/{session}?purge=true", method="DELETE",
                                       headers=inference_auth_headers())
        urllib.request.urlopen(purge, timeout=10).close()
    except Exception:
        pass
    return reply, latency_ms, truncated


def parse_anchors(reply: str) -> tuple[list[dict], bool]:
    """(anchors, schema_valid). A capped or malformed reply yields ([], False)."""
    try:
        data = json.loads(reply)
        entities = data["entities"]
        assert isinstance(entities, list)
    except (json.JSONDecodeError, KeyError, TypeError, AssertionError):
        return [], False
    return [{"name": e.get("name", ""), "types": e.get("types", []),
             "confidence": e.get("confidence"), "role": e.get("role"),
             "occurred_at": e.get("occurred_at")} for e in entities], True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="label for the result file (the server's "
                                                   "OLLAMA_MODEL decides what actually runs)")
    ap.add_argument("--url", default="http://localhost:8001")
    ap.add_argument("--gold", default=str(_eval_data("extraction/gold.jsonl", HERE / "gold.jsonl")))
    ap.add_argument("--sample", default=str(_eval_data("extraction/sample.jsonl", HERE / "sample.jsonl")))
    ap.add_argument("--limit", type=int, default=0,
                    help="FIRST N per set. Smoke-testing only — the corpus files are sorted "
                         "by doc_kind, so a head slice is badly skewed. Use --subset for a "
                         "real subset.")
    ap.add_argument("--subset", type=int, default=0,
                    help="a seeded RANDOM N per set — representative, and identical across "
                         "models and re-runs so results stay comparable (0 = all)")
    ap.add_argument("--subset-seed", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--run-dir", default="", help="directory for THIS run (checkpoint + "
                                                 "result). Derived from --out when omitted.")
    args = ap.parse_args()

    p = extract_pass()
    served = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{args.url}/health", headers=inference_auth_headers()), timeout=10).read())
    if served.get("model") != args.model:
        print(f"WARNING: server is serving {served.get('model')!r}, labelling {args.model!r}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = args.model.replace(":", "-").replace("/", "-")
    run_dir = Path(args.run_dir) if args.run_dir else Path(args.out) / f"{stamp}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Checkpoint every item as it lands. A few hundred items over a slow local model take
    # hours; writing only at the end means an interruption discards all of it. The same file
    # makes the run resumable: completed (set, id) pairs are
    # skipped, so a killed run continues instead of restarting.
    ckpt = run_dir / "items.jsonl"
    done: set = set()
    if ckpt.exists():
        with ckpt.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue                      # a torn final line from a hard kill
                done.add((rec["set"], rec["id"]))
        if done:
            print(f"resuming: {len(done)} item(s) already in {ckpt.name}")

    with ckpt.open("a") as sink:
        for set_name, path in (("gold", Path(args.gold)), ("sample", Path(args.sample))):
            records = [json.loads(line) for line in path.open()]
            if args.subset and args.subset < len(records):
                # Seeded on (seed, set) so the same flags pick the same records for EVERY
                # model — a subset comparison is only meaningful if both models saw the
                # same items. Random rather than a head slice because the corpus files are
                # sorted by doc_kind, so the first N is dominated by one or two kinds.
                records = random.Random(f"{args.subset_seed}:{set_name}").sample(
                    records, args.subset)
            elif args.limit:
                records = records[: args.limit]
            for i, rec in enumerate(records, 1):
                if (set_name, rec["id"]) in done:
                    continue
                user = render_template(p.user, rec)
                reply, latency_ms, truncated = call(args.url, p.system, user, p.schema,
                                                    args.timeout)
                anchors, valid = parse_anchors(reply)
                item = {"id": rec["id"], "set": set_name, "anchors": anchors,
                        "raw": reply, "schema_valid": valid, "truncated": truncated,
                        "latency_ms": latency_ms, "reply_chars": len(reply)}
                sink.write(json.dumps(item) + "\n")
                sink.flush()
                print(f"  [{set_name} {i}/{len(records)}] {len(anchors)} anchors "
                      f"{latency_ms}ms {'ok' if valid else 'INVALID'}"
                      f"{' TRUNCATED' if truncated else ''}", flush=True)

    items = [json.loads(line) for line in ckpt.open() if line.strip()]
    result = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "num_predict": served.get("num_predict"),
        "num_ctx": served.get("num_ctx"),
        "inputs": {"gold": args.gold, "sample": args.sample,
                   "gold_sha": sha(Path(args.gold)), "sample_sha": sha(Path(args.sample))},
        "items": items,
    }
    out_path = run_dir / "result.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"wrote {out_path} ({len(items)} items)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
