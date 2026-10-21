"""Pure scoring functions for the extraction eval. No I/O — tests need no live services.

Anchor matching is deliberately three-way. An exact match after normalization is a hit; a
close-but-different string is a NEAR MISS, counted as a miss for precision/recall but
reported separately, because "Avery Quinn" vs "Avery" is a different failure from
"kubernetes" vs "entity resolution" and the fix for each is different.
"""

from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher

# Above this similarity a non-exact pair is a near miss rather than an unrelated pair.
# 0.85 keeps singular/plural and short suffix drift together while separating distinct terms.
NEAR_MISS_RATIO = 0.85


def normalize(name: str) -> str:
    """Case-, accent-, and punctuation-insensitive key for anchor comparison.

    Apostrophes are DELETED ("Avery's" -> "averys") while every other non-alphanumeric —
    underscore included, since `\\w` keeps it — becomes a space ("KB_Core" -> "kb core").
    Both spellings of the same thing must land on one key or the eval double-counts them.
    """
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", "").replace("'", "")
    s = re.sub(r"[\W_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_anchors(predicted: list[str], gold: list[str]) -> dict:
    """Align predicted anchors to gold ones. Each gold anchor is consumed at most once.

    Exact normalized matches are taken first so a near miss can never steal a gold anchor
    that some other prediction matches exactly.
    """
    remaining = list(gold)
    hits: list[tuple[str, str]] = []
    near: list[tuple[str, str]] = []
    unmatched: list[str] = []

    for p in predicted:
        np = normalize(p)
        exact = next((g for g in remaining if normalize(g) == np), None)
        if exact is not None:
            remaining.remove(exact)
            hits.append((p, exact))
        else:
            unmatched.append(p)

    false_positives: list[str] = []
    for p in unmatched:
        np = normalize(p)
        best, best_ratio = None, 0.0
        for g in remaining:
            ratio = SequenceMatcher(None, np, normalize(g)).ratio()
            if ratio > best_ratio:
                best, best_ratio = g, ratio
        if best is not None and best_ratio >= NEAR_MISS_RATIO:
            remaining.remove(best)
            near.append((p, best))
        else:
            false_positives.append(p)

    return {"hits": hits, "near_misses": near,
            "false_positives": false_positives, "misses": remaining}


def prf(matched: dict) -> dict:
    """Precision / recall / F1 from a match_anchors result. Near misses count as misses."""
    tp = len(matched["hits"])
    predicted = tp + len(matched["near_misses"]) + len(matched["false_positives"])
    actual = tp + len(matched["near_misses"]) + len(matched["misses"])
    precision = tp / predicted if predicted else 0.0
    recall = tp / actual if actual else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def hit_accuracy(pairs: list[tuple[dict, dict]]) -> dict:
    """Type / role / occurred_at correctness over (predicted, gold) anchors that matched.

    Only matched anchors are scored. An anchor that shouldn't exist at all is already a
    false positive in prf(); grading its type here would punish one error twice.

    Types get both a strict score (`type_exact`, set equality) and a partial one
    (`type_jaccard`). The prompt permits multiple true types, so "right type plus a spurious
    extra" is a milder, different failure from "wrong type" and shouldn't collapse into it.

    role/occurred_at are scored only where GOLD says the field is due. A field emitted where
    the memory never justified one is counted as `*_spurious`, not folded into accuracy —
    over-emitting and under-emitting call for opposite prompt fixes.
    """
    n = len(pairs)
    if not n:
        return {"n": 0, "type_exact": 0.0, "type_jaccard": 0.0,
                "role_n": 0, "role_accuracy": 0.0, "role_spurious": 0,
                "occurred_at_n": 0, "occurred_at_accuracy": 0.0, "occurred_at_spurious": 0}

    exact = jaccard = 0.0
    tally = {"role": [0, 0, 0], "occurred_at": [0, 0, 0]}      # [due, correct, spurious]
    for pred, gold in pairs:
        pt = {normalize(t) for t in pred.get("types", [])}
        gt = {normalize(t) for t in gold.get("types", [])}
        exact += 1.0 if pt == gt else 0.0
        union = pt | gt
        jaccard += (len(pt & gt) / len(union)) if union else 1.0
        for field in ("role", "occurred_at"):
            pv, gv = pred.get(field), gold.get(field)
            if gv:
                tally[field][0] += 1
                tally[field][1] += 1 if pv == gv else 0
            elif pv:
                tally[field][2] += 1

    out = {"n": n, "type_exact": exact / n, "type_jaccard": jaccard / n}
    for field in ("role", "occurred_at"):
        due, correct, spurious = tally[field]
        out[f"{field}_n"] = due
        out[f"{field}_accuracy"] = (correct / due) if due else 0.0
        out[f"{field}_spurious"] = spurious
    return out


# --- proxy metrics (no gold labels needed) -----------------------------------------

def confidence_spread(anchors: list[dict]) -> dict:
    """How much of the confidence enum the model actually uses.

    A model can collapse the three-level enum to one value — an enum used as a constant,
    which nothing downstream can gate on. `top_share` near 1.0 means the field carries no
    information.
    """
    counts: dict[float, int] = {}
    for a in anchors:
        c = a.get("confidence")
        if c is not None:
            counts[c] = counts.get(c, 0) + 1
    total = sum(counts.values())
    return {
        "counts": counts,
        "levels_used": len(counts),
        "top_share": (max(counts.values()) / total) if total else 0.0,
    }


def conditional_fill(anchors: list[dict], kind_of: dict[str, str]) -> dict:
    """`role` / `occurred_at` fill rates against the kind each field is actually due on.

    The denominator matters: raw fill over all anchors reads near-zero simply because most
    anchors are concepts, which are never supposed to carry either field.
    """
    by_kind: dict[str, list[dict]] = {}
    for a in anchors:
        kinds = {kind_of.get(t) for t in a.get("types", [])} - {None}
        for k in kinds:
            by_kind.setdefault(k, []).append(a)
    out = {}
    for kind, items in by_kind.items():
        n = len(items)
        out[kind] = {
            "n": n,
            "role": sum(1 for a in items if a.get("role")) / n,
            "occurred_at": sum(1 for a in items if a.get("occurred_at")) / n,
        }
    return out


def duplicate_rate(anchors: list[dict]) -> float:
    """Share of anchors that collide with an earlier one after normalization.

    The prompt forbids emitting two spelling variants of one thing as separate anchors, so
    this is a direct instruction-adherence signal.
    """
    if not anchors:
        return 0.0
    seen: set[str] = set()
    dupes = 0
    for a in anchors:
        key = normalize(a["name"])
        if key in seen:
            dupes += 1
        seen.add(key)
    return dupes / len(anchors)


def percentiles(values: list[float]) -> dict:
    """p50 / p90 / p99 by nearest-rank. Empty series reads as zeros.

    Nearest-rank is ceil(q*n)-1, not int(q*n) — the latter is off by one at every exact
    boundary (it reports 51 as the median of 1..100).
    """
    if not values:
        return {"p50": 0.0, "p90": 0.0, "p99": 0.0}
    s = sorted(values)
    def at(q: float):
        return s[max(0, math.ceil(q * len(s)) - 1)]
    return {"p50": at(0.50), "p90": at(0.90), "p99": at(0.99)}
