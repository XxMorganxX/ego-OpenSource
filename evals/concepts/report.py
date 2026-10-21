#!/usr/bin/env python3
"""Render the entity-weighting sweep as a standalone measurement note.

    uv run --project KB_Core python -m evals.concepts.report > \
        entity-weighting-measurement.md

The numbers come from the sweep's results file; the prose is a fixed guide to reading
them. Write the note into the repo's docs rather than leaving the figures in a chat or a
private note: an undocumented ablation figure rots unnoticed while still being cited as
current.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HEAD = "| α | blocklist | variant | res | concepts | ent share | recall | lift | z |"
RULE = "|---|---|---|---|---|---|---|---|---|"


def _alpha_cell(alpha: "float | None") -> str:
    """True-ablation arms carry no alpha at all -- there is no mention-edge weight to mix
    because there are no mention edges. Render that as an em dash, not the string "None"."""
    return "—" if alpha is None else str(alpha)


def _sort_key(row: dict) -> tuple:
    """(blocklist, variant, alpha) with `alpha: None` (the true-ablation arms) bucketed
    before any float via an explicit `is None` flag, rather than relying on `blocklist`/
    `variant` differing enough that tuple comparison never reaches `alpha`. It happened to
    hold -- `blocklist="n/a"` is unique -- but a second None-alpha family sharing a
    blocklist/variant with a real-alpha arm would raise `TypeError: '<' not supported
    between instances of 'NoneType' and 'float'`."""
    return (row["blocklist"], row["variant"], row["alpha"] is None,
            row["alpha"] if row["alpha"] is not None else 0.0)


def render(result: dict) -> str:
    b = result["baseline"]
    out = [
        "# Entity edge weighting — sweep results",
        "",
        f"**Snapshot:** {result['snapshot'].get('captured_at')}  ",
        f"**Snapshot sha256:** `{result['snapshot_sha256']}`  ",
        f"**Judge:** pairwise co-membership over {result['n_pairs']} usable golden pairs  ",
        "",
        "## Baseline (α=1, blocklist as-is, resolution 1.0)",
        "",
        f"- `pos_recall` **{b['pos_recall']:.3f}** ({b['hits']}/{b['n_pairs']})",
        f"- null mean {b['null_mean']:.3f}, sd {b['null_sd']:.3f} → "
        f"**lift {b['lift']:.3f}**, z {b['z']:+.1f}",
        f"- {b['n_concepts']} level-0 concepts",
        f"- headroom: **{len(b['missed_pairs'])} pairs**",
        "",
        "## Arms",
        "",
        HEAD, RULE,
    ]
    for a in sorted(result["arms"], key=_sort_key):
        out.append(
            f"| {_alpha_cell(a['alpha'])} | {a['blocklist']} | {a['variant']} "
            f"| {a['resolution']:.3f} | {a['n_concepts']} | {a['entity_share']:.3f} "
            f"| {a['pos_recall']:.3f} | {a['lift']:+.3f} | {a['z']:+.1f} |")
    out += [
        "",
        "## How to read this",
        "",
        "- **The `true_ablation` arms are the structural ablation** — `partition([], knn, "
        "...)`, no entity vertices in the graph at all, in both the `raw` and "
        "`granularity-matched` variants. Compare them with the α=1.0 baseline: the same "
        "`pos_recall` with the same missed pairs means removing the entity layer changes no "
        "golden co-membership pair. It does not mean the partition is unchanged — a coarse "
        "pair judge can miss changes that land outside the concepts its pairs cover — so "
        "diff the memory-to-concept assignment separately before calling the layer inert.",
        "- **α=0 is not an ablation and must not be read as one.** `build_graph` registers a "
        "mention edge's entity vertex before any weight check runs, so `entity_alpha=0` "
        "still leaves entity vertices and zero-weight edges in the graph — only the entity "
        "*weight* is zeroed, not the *vertex*. That shifts the name-sorted vertex order "
        "Leiden's local moving visits, so α=0 `as-is` and `restored` can disagree while "
        "carrying the same zero weight. A granularity-matched α=0 cell compounds this: "
        "`match_resolution`'s bisection fits a resolution to that degenerate graph, and the "
        "cell can land far outside the noise floor without saying anything about the "
        "mixing ratio.",
        "- **Noise floor.** The *raw*-variant α=0 contrast (`as-is` vs `restored`) estimates "
        "the vertex-order noise floor. A recall band across the α sweep that stays inside "
        "it licenses no conclusion about the mixing ratio in either direction: \"flat "
        "within noise\" is not \"measured to have no effect\". The metric also saturates "
        "quickly, so arms sharing a `pos_recall` is the expected outcome of a mostly-solved "
        "judge, not evidence of anything in particular.",
        "- **blocklist `restored` vs `as-is`** asks whether the layer is starved rather than "
        "unhelpful. It is a simulation: synthetic edges from `entity_candidates` skip the "
        "meaning judge, alias resolution and merge folding, so it gives a directional hint "
        "at most.",
        "- **`raw` vs `granularity-matched`** separates a ratio effect from a resolution "
        "effect. If an arm's gain vanishes under matching, the gain came from resolution "
        "rather than from the entity:embedding ratio.",
        "- **No ship bar applies here.** The arms are descriptive measurements, not a "
        "competition. If a future sweep shows a real curve, expand the golden set first "
        "and pre-register a bar the sample can support; never weaken a bar after seeing "
        "the data.",
        "",
        "## Coverage caveat",
        "",
        "Count how many of the baseline's level-0 concepts the satisfied pairs land in and "
        "cite that figure — level-0 is the granularity the baseline and arms are scored at. "
        "A count over all levels is a different, finer-grained population and must not be "
        "paired with the level-0 denominator. Conclusions only cover the concepts the pairs "
        "touch. Also report golden-set rot: how many of the ids the must-only pairs "
        "reference are no longer live (a wider count that includes the unused negatives is "
        "a different population — say which one you quote).",
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", default="evals/concepts/results/sweep.json")
    args = p.parse_args()
    print(render(json.loads(Path(args.results).read_text())), end="")


if __name__ == "__main__":
    main()
