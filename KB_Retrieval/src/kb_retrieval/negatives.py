"""Measured negatives -- ideas built, measured, and found harmful.

Promoted from code comments to a module because two strategies can now rediscover
the same bad idea independently. Each entry names its evidence, its n, and the
condition under which it is worth re-testing, so a settled question is
distinguishable from a stale one.

`n` is the size of the evaluation set a measurement ran on. The values shipped here
are placeholders (1), not the original evaluation sizes; when you re-run a
measurement on your own corpus, record your own n.

Do not delete an entry because it sounds wrong. Re-run its measurement.

Each entry records the mechanism and the direction of the effect, which are what
carry over from the corpus it was measured on to another one.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Negative:
    claim: str
    measurement: str
    n: int  # evaluation-set size; placeholder unless you record your own
    retest_trigger: str
    citation: str


MEASURED_NEGATIVES = {
    "similarity_floor": Negative(
        claim="Filter rows below a cosine floor to raise precision.",
        measurement="Built at 0.50. must_recall fell, and some golden items lost "
                    "their gold outright. Per-row gold and off-corpus distributions "
                    "overlap: gold's low tail sits BELOW the off-corpus median, so no "
                    "floor separates them.",
        n=1,
        retest_trigger="A change of embedding model. (Not re-tested after the query "
                       "instruction was added: the query-side distribution moved, but "
                       "the trigger -- an embedding-model change -- did not fire.)",
        citation="knowledge_base.py CONFIDENCE_* note",
    ),
    "margin_signal": Negative(
        claim="Use distribution shape (top1 minus median) instead of a raw threshold.",
        measurement="The largest off-corpus margin EXCEEDS the gold median margin. "
                    "Measured worse than the raw threshold it was meant to improve on.",
        n=1,
        retest_trigger="A change of embedding model. (Not re-tested after the query "
                       "instruction was added: the query-side distribution moved, but "
                       "the trigger -- an embedding-model change -- did not fire.)",
        citation="knowledge_base.py CONFIDENCE_* note",
    ),
    "query_prefix": Negative(
        claim="Apply qwen3's Instruct:/Query: prefix to the query vector used for "
              "LABEL-COSINE concept entry routing.",
        measurement="Entry routing top-1 and top-3 both fell. Two independent "
                    "phrasings, same sign and magnitude. "
                    "Scope: this measured concept LABEL routing only. For "
                    "document retrieval the prefix is the model's designed asymmetric "
                    "use and measured strongly positive; "
                    "production applies it to the retrieval legs and keeps label "
                    "routing bare (Query.label_embedding).",
        n=1,
        retest_trigger="Any embedder change, or re-embedding the corpus WITH a "
                       "document-side prefix. The sign is trustworthy; the magnitude "
                       "is thin.",
        citation="crawl/entry.py module docstring",
    ),
    "p90_hub_gate": Negative(
        claim="Block traversal through entities above p90 distinct-memory support, "
              "to prevent hub attraction.",
        measurement="Anti-subject, not anti-hub. A p90 support gate blocks the "
                    "most-referenced subjects in the golden set -- the corpus's own "
                    "main projects and tools. Present tense, not future drift.",
        n=1,
        retest_trigger="A corpus whose entity support distribution is no longer "
                       "dominated by its own subjects.",
        citation="crawl/frontier.py BRIDGE_FANOUT note",
    ),
    "agentic_hop_judge": Negative(
        claim="Use an LLM (LLMHopJudge) for batched aboutness/package-membership "
              "admission per crawl hop, instead of the deterministic first-N rule.",
        measurement="Built, measured in a 2x2 benchmark over both golden sets with "
                    "repeats, then deleted. agentic_fraction-gated (nearly every hop "
                    "was really decided by the LLM): SET-FLAT recall fell below the "
                    "deterministic rule's by more than the run-to-run spread, "
                    "precision no better, neg_hits no better; SET-PACKAGE a three-way "
                    "tie. Cost: one provider call per hop, and latency orders of "
                    "magnitude above the deterministic crawl's.",
        n=1,
        retest_trigger="The backend becomes a parallel API (the design already reserved "
                       "this), the corpus grows ~10x so reachability stops deciding "
                       "recall, or a judge model materially cheaper/faster than claude -p "
                       "subprocess-per-call. Resurrection is additive: one HOP_JUDGES entry "
                       "plus a judge module behind the HopJudge Protocol.",
        citation="crawl/judge.py note above HOP_JUDGES (the design's "
                 "pre-registered falsification condition was met)",
    ),
}
