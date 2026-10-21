"""Positive co-membership pairs mined from the retrieval golden set.

Every golden item's `must` memory set is a human-authored assertion that those memories
belong together. Read pairwise it becomes a grouping judge that is independent of the
embedding space (the expectations were hand-written, the embedder did not write them) and independent of
concept names (so no LLM labeling is needed to score an arm).

The `negatives` lists are deliberately NOT used: the partition separates them worse than
its random null, systematically. A retrieval distractor is topically adjacent by construction, which is exactly what makes it
a distractor, so it reliably lands in the same concept. The labels are inverted, not noisy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default


GOLDEN = _eval_data("retrieval/golden.jsonl",
                     Path(__file__).resolve().parents[1] / "retrieval" / "golden.jsonl")


def authored_pairs(path: "Path | None" = None) -> "set[tuple[str, str]]":
    """Unique unordered memory-id pairs co-asserted by some item's must-set."""
    path = Path(path) if path is not None else GOLDEN
    pairs: "set[tuple[str, str]]" = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        expected = json.loads(line).get("expected", {})
        must = [m["id"] for m in expected.get("memories", []) if m.get("must")]
        for i in range(len(must)):
            for j in range(i + 1, len(must)):
                pairs.add(tuple(sorted((must[i], must[j]))))
    return pairs


def usable_pairs(pairs: "set[tuple[str, str]]",
                 live_ids: "set[str]") -> "set[tuple[str, str]]":
    """Pairs whose BOTH memories still exist and are not dead. A pair touching a dead
    memory is unscoreable, not a miss -- dropping it is the honest treatment."""
    return {(a, b) for a, b in pairs if a in live_ids and b in live_ids}
