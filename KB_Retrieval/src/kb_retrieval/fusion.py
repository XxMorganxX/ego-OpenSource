"""Reciprocal rank fusion. One implementation, every strategy.

Rank-based fusion is the reason mode A never calibrates its two arms: ts_rank and
cosine are incomparable quantities, but their POSITIONS are not. Every list carries
weight 1.0 -- there are no weight constants in this module, and adding one would
reintroduce exactly the tuning surface RRF was chosen to avoid.

Proven equivalent to the SQL fusion it replaces across every golden query;
both strategies depend on that single proof.
"""

from collections.abc import Sequence
from uuid import UUID

from kb_core.knowledge_base import RRF_K


def rrf(lists: Sequence[Sequence[UUID]], k: int = RRF_K) -> dict[UUID, float]:
    """Fuse ranked id lists. Absence from a list contributes 0, never a penalty --
    that is what lets an arm-exclusive hit still score (mode A's FULL OUTER JOIN).

    A duplicate id within one list takes its best (first) position.
    """
    scores: dict[UUID, float] = {}
    for ranked in lists:
        seen: set[UUID] = set()
        for i, ident in enumerate(ranked, start=1):
            if ident in seen:
                continue
            seen.add(ident)
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k + i)
    return scores
