#!/usr/bin/env python3
"""Adjudicate extraction benchmark arms against each other with a pinned Claude judge.

WHY THIS EXISTS
`run.py` records what each model emitted. Nothing in that record says whether an anchor is
*right*: a fabricated anchor and a correct one are the same shape, and an anchor the model
never found leaves no trace at all. Anchor counts can't separate the two — 21 anchors against
28 is either better precision or worse recall, and the raw items cannot tell you which.

HOW IT SCORES WITHOUT AN ANNOTATED GOLD SET
Pooling, the standard trick from IR evaluation. A head-to-head does not need absolute ground
truth; it needs a reference set that is fair to every arm. For each memory we take the UNION
of every arm's anchors, have the judge mark each one valid or invalid against the production
rubric, and use the judged-valid union as the reference. An arm's recall against that pool
answers the question actually being asked — "does the 12B find what the 26B finds?" — with no
human annotation at all.

What pooling cannot see is an anchor that EVERY arm missed. The judge is therefore also asked
to propose anchors none of the arms produced, which is reported separately as
`extended_recall`. Read the two differently: `pooled_recall` is a fair comparison between
arms, `extended_recall` leans on the judge's own proposals and is the softer number.

When `gold_anchors` is populated in the frozen corpus (hand annotation), this
additionally reports true precision/recall/F1 against it. Until then that block is absent
rather than zero — an unannotated corpus must not read as a score of 0.0.

THREE THINGS THAT KEEP THE VERDICT HONEST
- BLIND. The pooled anchors are shuffled and carry no indication of which arm produced them,
  so the judge cannot favour a model it can identify. The shuffle is seeded per item, so a
  re-run adjudicates the same list in the same order.
- REASONING BEFORE VERDICT. Each verdict carries a `reasoning` field the judge is told to
  write FIRST. Structured output is the judge's only channel, so this is what "thinking" means
  here — a bare boolean is a snap judgment, and the schema is what forces the working out.
- PINNED AND CONSTANT. One judge model, one prompt, for every arm; the model is recorded in
  the output. A judge that varied between arms would be measuring itself.

The rubric is not copied here — it is read live from the production system prompt, so the
judge grades against the same instructions the models were given.

Usage:
  judge.py --run-dir runs/<stamp> [--arms slug,slug] [--limit N]
Run with KB_Core's venv python (needs kb_core for the Claude CLI client).
"""
import argparse
import json
import os
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from kb_core.ingest.claude_cli_client import ClaudeCliInferenceClient

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from metrics import match_anchors, normalize, prf  # noqa: E402


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default

# Pinned so a re-run is comparable. Overridable for a deliberate judge-sensitivity check —
# changing it invalidates comparison against previously scored runs, which is why the model
# is written into scores.json rather than left implicit.
JUDGE_MODEL = os.getenv("KB_EXTRACTION_JUDGE_MODEL", "claude-sonnet-5")

PRODUCTION_RUBRIC = Path(
    os.getenv("KB_ENTITIES_PROMPT",
              str(HERE / "../../KB_Core/prompts/entities.system.md"))).resolve()

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "valid": {"type": "boolean"},
                },
                "required": ["name", "reasoning", "valid"],
            },
        },
        "missing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["name", "reasoning"],
            },
        },
    },
    "required": ["verdicts", "missing"],
}

JUDGE_SYSTEM = """\
You are grading candidate retrieval anchors that several extraction models proposed for one
memory. Below is the EXACT instruction those models were given. Grade against it — not against
your own taste in anchors.

════════ THE RUBRIC THE MODELS WERE GIVEN ════════
{rubric}
════════ END OF RUBRIC ════════

You will be given the memory and a list of candidate anchors pooled from several models. The
list is shuffled and anonymous; you cannot tell which model proposed what, and you must not
speculate about it.

For EVERY candidate, return a verdict:
- Write `reasoning` FIRST — one sentence citing what in the memory does or does not support
  the anchor. Then set `valid`. Do not decide before you have written the reasoning.
- `valid: true` if the rubric would have the extractor emit this anchor for this memory.
- `valid: false` if it is fabricated, unsupported by the memory, incidental vocabulary, a
  duplicate of another candidate, a whole-sentence name, a subcommand or flag, or a generic
  platitude that would not discriminate between notes.

Judge each candidate on its own merit. The number of valid anchors is whatever the memory
justifies — do not aim for a target count, and do not balance true against false.

Then fill `missing` with anchors the rubric clearly requires that NO candidate covers. Leave
it empty if the candidates already cover the memory; do not pad it. These are anchors every
model missed, so hold them to the same standard you applied above.

Return every candidate exactly once in `verdicts`, using the candidate's name verbatim."""

JUDGE_USER = """\
MEMORY
Title: {title}
Filed under: {location}

{content}

CANDIDATE ANCHORS ({n})
{candidates}"""


def load_arms(run_dir: Path, arms: "list[str] | None") -> "dict[str, dict]":
    """{arm_slug: {(set, id): item}} from each arm's items.jsonl.

    Torn final lines from a killed run are skipped, matching run.py's checkpoint reader.
    """
    out: "dict[str, dict]" = {}
    for sub in sorted(run_dir.iterdir()):
        ckpt = sub / "items.jsonl"
        if not sub.is_dir() or not ckpt.exists():
            continue
        if arms and sub.name not in arms:
            continue
        items = {}
        with ckpt.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                items[(rec["set"], rec["id"])] = rec
        if items:
            out[sub.name] = items
    return out


def pool_candidates(arm_items: "dict[str, dict]", key: tuple, seed: int) -> list[str]:
    """The shuffled, deduped union of every arm's anchor names for one item.

    Deduped on normalize(), so two arms spelling the same anchor differently are graded once.
    The surviving display name is the most common spelling across arms, ties broken
    alphabetically — deterministic, and chosen without reference to WHICH arm supplied it so
    the display name cannot leak provenance to the judge.
    """
    spellings: "dict[str, Counter]" = {}
    for items in arm_items.values():
        rec = items.get(key)
        if not rec:
            continue
        for anchor in rec.get("anchors", []):
            name = (anchor.get("name") or "").strip()
            if not name:
                continue
            spellings.setdefault(normalize(name), Counter())[name] += 1

    names = [sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
             for c in spellings.values()]
    names.sort()                                   # stable base order before the seeded shuffle
    random.Random(f"{seed}:{key[0]}:{key[1]}").shuffle(names)
    return names


def build_prompts(record: dict, candidates: list[str], rubric: str) -> "tuple[str, str]":
    """(system, user) for one item's adjudication."""
    listing = "\n".join(f"- {n}" for n in candidates)
    return (
        JUDGE_SYSTEM.format(rubric=rubric.strip()),
        JUDGE_USER.format(title=record.get("title", ""),
                          location=record.get("location", ""),
                          content=record.get("content", ""),
                          n=len(candidates), candidates=listing),
    )


def parse_verdict(reply: str, candidates: list[str]) -> "dict | None":
    """Judge reply -> {valid: [names], invalid: [names], missing: [names]}, or None.

    A verdict for a name the judge was not asked about is dropped, and a candidate the judge
    failed to return is left UNJUDGED rather than defaulted — silently treating an omission
    as invalid would deflate precision for whichever arm proposed it.
    """
    try:
        data = json.loads(reply)
        verdicts = data["verdicts"]
        missing = data.get("missing", [])
        assert isinstance(verdicts, list) and isinstance(missing, list)
    except (json.JSONDecodeError, KeyError, TypeError, AssertionError):
        return None

    asked = {normalize(c): c for c in candidates}
    valid, invalid, seen = [], [], set()
    for v in verdicts:
        key = normalize(str(v.get("name", "")))
        if key not in asked or key in seen:
            continue
        seen.add(key)
        (valid if v.get("valid") else invalid).append(asked[key])
    return {
        "valid": valid,
        "invalid": invalid,
        "unjudged": [asked[k] for k in asked if k not in seen],
        "missing": [str(m.get("name", "")).strip()
                    for m in missing if str(m.get("name", "")).strip()],
        "reasoning": {normalize(str(v.get("name", ""))): v.get("reasoning", "")
                      for v in verdicts},
    }


def score_arms(arm_items: "dict[str, dict]", judgments: "dict[tuple, dict]",
               corpus: "dict[tuple, dict]") -> dict:
    """Per-arm precision / pooled recall / extended recall, micro-averaged over items.

    Micro rather than macro: an anchor is the unit of interest, and macro-averaging would let
    a one-anchor memory outweigh a ten-anchor one.
    """
    scores = {}
    for arm, items in arm_items.items():
        emitted = valid_found = 0
        pool_total = ext_total = ext_found = 0
        gold_pred: "list[str]" = []
        gold_ref: "list[str]" = []
        for key, judged in judgments.items():
            rec = items.get(key)
            if rec is None:
                continue
            names = [(a.get("name") or "").strip() for a in rec.get("anchors", [])]
            names = [n for n in names if n]
            mine = {normalize(n) for n in names}

            ref = {normalize(n) for n in judged["valid"]}
            ext = ref | {normalize(n) for n in judged["missing"]}

            emitted += len(mine)
            valid_found += len(mine & ref)
            pool_total += len(ref)
            ext_total += len(ext)
            ext_found += len(mine & ext)

            gold = corpus.get(key, {}).get("gold_anchors") or []
            if gold:
                gold_pred += names
                gold_ref += [g["name"] if isinstance(g, dict) else str(g) for g in gold]

        entry = {
            "items_scored": sum(1 for k in judgments if k in items),
            "anchors_emitted": emitted,
            "anchors_judged_valid": valid_found,
            # Of what this arm emitted, how much the judge accepted.
            "precision": (valid_found / emitted) if emitted else 0.0,
            # Of the judged-valid union across arms, how much this arm found. The fair
            # head-to-head number.
            "pooled_recall": (valid_found / pool_total) if pool_total else 0.0,
            # Same, plus anchors the judge said every arm missed. Softer — it rests on the
            # judge's own proposals rather than on anything a model produced.
            "extended_recall": (ext_found / ext_total) if ext_total else 0.0,
        }
        p, r = entry["precision"], entry["pooled_recall"]
        entry["pooled_f1"] = (2 * p * r / (p + r)) if (p + r) else 0.0
        if gold_ref:
            entry["vs_gold"] = prf(match_anchors(gold_pred, gold_ref))
        scores[arm] = entry
    return scores


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, help="a runs/<stamp> directory")
    ap.add_argument("--arms", default="", help="comma-separated arm slugs (default: all)")
    ap.add_argument("--gold", default=str(_eval_data("extraction/gold.jsonl", HERE / "gold.jsonl")))
    ap.add_argument("--sample", default=str(_eval_data("extraction/sample.jsonl", HERE / "sample.jsonl")))
    ap.add_argument("--limit", type=int, default=0, help="adjudicate at most N items")
    ap.add_argument("--shuffle-seed", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    arms = [a for a in args.arms.split(",") if a] or None
    arm_items = load_arms(run_dir, arms)
    if not arm_items:
        raise SystemExit(f"no arms with items.jsonl under {run_dir}")

    corpus = {}
    for set_name, path in (("gold", Path(args.gold)), ("sample", Path(args.sample))):
        for line in Path(path).open():
            rec = json.loads(line)
            corpus[(set_name, rec["id"])] = rec

    rubric = PRODUCTION_RUBRIC.read_text()
    client = ClaudeCliInferenceClient(model=JUDGE_MODEL, timeout=args.timeout,
                                      hardened=True)

    keys = sorted({k for items in arm_items.values() for k in items})
    if args.limit:
        keys = keys[: args.limit]
    print(f"arms: {', '.join(sorted(arm_items))}")
    print(f"items to adjudicate: {len(keys)}  judge: {JUDGE_MODEL}")

    # Checkpointed for the same reason run.py is: adjudication is minutes of provider calls,
    # and an interruption must not discard all of it.
    ckpt = run_dir / "judgments.jsonl"
    judgments: "dict[tuple, dict]" = {}
    if ckpt.exists():
        with ckpt.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                judgments[(rec["set"], rec["id"])] = rec["judgment"]
        print(f"resuming: {len(judgments)} item(s) already judged")

    with ckpt.open("a") as sink:
        for i, key in enumerate(keys, 1):
            if key in judgments:
                continue
            record = corpus.get(key)
            if record is None:
                print(f"  [{i}/{len(keys)}] {key[1][:8]} SKIP — not in frozen corpus")
                continue
            candidates = pool_candidates(arm_items, key, args.shuffle_seed)
            if not candidates:
                print(f"  [{i}/{len(keys)}] {key[1][:8]} SKIP — no arm emitted an anchor")
                continue
            system, user = build_prompts(record, candidates, rubric)
            try:
                reply = client.complete(system, user, schema=JUDGE_SCHEMA)
            except Exception as exc:                      # provider/transport failure
                print(f"  [{i}/{len(keys)}] {key[1][:8]} JUDGE FAILED: {exc}")
                continue
            judged = parse_verdict(reply, candidates)
            if judged is None:
                print(f"  [{i}/{len(keys)}] {key[1][:8]} UNPARSEABLE verdict")
                continue
            judgments[key] = judged
            sink.write(json.dumps({"set": key[0], "id": key[1],
                                   "judgment": judged}) + "\n")
            sink.flush()
            print(f"  [{i}/{len(keys)}] {key[1][:8]} "
                  f"{len(judged['valid'])}/{len(candidates)} valid, "
                  f"{len(judged['missing'])} missed by all"
                  + (f", {len(judged['unjudged'])} UNJUDGED" if judged["unjudged"] else ""),
                  flush=True)

    scores = score_arms(arm_items, judgments, corpus)
    out = {
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "judge_model": JUDGE_MODEL,
        "rubric_source": str(PRODUCTION_RUBRIC),
        "items_adjudicated": len(judgments),
        "gold_annotated": any((corpus.get(k, {}).get("gold_anchors") or [])
                              for k in judgments),
        "arms": scores,
    }
    (run_dir / "scores.json").write_text(json.dumps(out, indent=2))

    print(f"\n{'arm':<28} {'emitted':>8} {'prec':>7} {'pool_rec':>9} {'ext_rec':>8} {'F1':>7}")
    for arm, s in sorted(scores.items(), key=lambda kv: -kv[1]["pooled_f1"]):
        print(f"{arm:<28} {s['anchors_emitted']:>8} {s['precision']:>7.3f} "
              f"{s['pooled_recall']:>9.3f} {s['extended_recall']:>8.3f} "
              f"{s['pooled_f1']:>7.3f}")
    if not out["gold_annotated"]:
        print("\nNOTE: gold.jsonl carries no annotations, so these are RELATIVE scores — "
              "they rank the arms against each other, not against truth.")
    print(f"\nwrote {run_dir / 'scores.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
