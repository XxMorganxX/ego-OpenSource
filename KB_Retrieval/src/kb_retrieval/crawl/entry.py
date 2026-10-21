"""Entry step: A[:3] label-cosine  u  C_damped[:2] evidence-vote  u  N[:2] name-anchor.

This module returns CONCEPT ids and ENTITY ids to expand FROM -- never a memory id.
The concept layer is the ROUTER, never the reachable set. The
non-negotiable THIRD seed, hop 0, is the flat HybridStrategy pool; it is not built
here because it needs no embedding-routing at all. See strategy.py's use of
HybridStrategy.flat_pool().

MEASURED: label cosine alone and the damped evidence vote alone each route worse
than the UNION A[:3] u C[:2], which comes close to the ceiling of routing through
every active concept. The union is load-bearing -- either family alone is measurably
worse -- and on A u C alone the entry step captures nearly everything concept
membership can reach. The name anchor is load-bearing separately: a minority of
golden items resolve an entity first, and most of the items concept routing misses
are entity-routed.

No entry floor, and no prefix on the vector this module reads. Both are recorded
MEASURED_NEGATIVES (kb_retrieval.negatives): a cosine floor on the entry
distribution is a new uncalibrated threshold on a distribution with no gold, and the
qwen3 query-side prefix measured entry routing DOWN (top-1 and top-3) because
every stored vector was embedded prefix-free. That negative is now SCOPED to
label-cosine routing, not the query as a whole: concept label
matching and the name anchor read Query.label_vector() -- the bare vector -- while
`embedding` itself may carry kb_core.embedding.QUERY_INSTRUCTION for every
query-vs-document consumer elsewhere (kNN legs, similarity_for, bridge_frontier).
Re-run the measurement before reintroducing either; do not re-add them because they
sound plausible.

If every family is empty the crawl degenerates to the flat pool and says so
(strategy.py sets trace.degraded["entry"]). That is a disclosed outcome, not a
failure.
"""

from dataclasses import dataclass
from uuid import UUID

from kb_retrieval.contract import Query

ENTRY_CONCEPT_LABEL_TOP = 3     # A[:3]
ENTRY_CONCEPT_VOTE_TOP = 2      # C_damped[:2]
ENTRY_NAME_ANCHOR_TOP = 2       # N[:2]


@dataclass(frozen=True)
class EntrySeed:
    kind: str    # "concept" | "entity"
    id: UUID
    via: str     # "label-cosine" | "evidence-vote" | "name" | "fuzzy-name" | "anchor_entity"
    label: str


def entry_step(kb, q: Query, conn, *, skip_label_cosine: bool = False) -> "list[EntrySeed]":
    """Route `q` to at most 7 anchors. `conn` is the caller's pinned snapshot; both
    primitives used here accept it, so the entry step reads the same DB state as
    every other call in the same search().

    `skip_label_cosine`, when True, skips the A rung (label-cosine) ENTIRELY --
    the benchmark's `ablate={"label-cosine"}` knob. This must be a skip, not a
    post-hoc filter on the returned seeds: `_add` below is first-wins on
    `(kind, id)`, so a concept both the A rung and the C rung (evidence-vote) would
    independently pick is stored under whichever rung runs first. The A rung runs
    first, so a caller that instead filtered `via == "label-cosine"` out of the
    result would also drop a concept the C rung earned on its own -- ablating part
    of the damped evidence vote too. Skipping the rung lets the C rung (or the name
    anchor) claim that concept under its own `via` if it independently qualifies.
    Default False is behaviour-preserving.
    """
    seeds: "list[EntrySeed]" = []
    seen: "set[tuple[str, UUID]]" = set()

    def _add(kind, ident, via, label):
        key = (kind, ident)
        if key not in seen:
            seen.add(key)
            seeds.append(EntrySeed(kind=kind, id=ident, via=via, label=label))

    # A pre-resolved anchor needs no vector, so it is honoured even with the embedder
    # down -- and it goes FIRST so an explicitly supplied anchor keeps its own `via`
    # tag when a later rung would otherwise re-add the same id.
    if q.anchor_entity is not None:
        _add("entity", q.anchor_entity, "anchor_entity", "")

    # Concept label routing and the entity name rungs match the query against LABELS
    # and NAMES, not documents: they keep the bare query vector (see
    # negatives.py MEASURED_NEGATIVES["query_prefix"]).
    label_vec = q.label_vector()
    if label_vec is not None:
        concepts = kb.concept_candidates(label_vec, conn=conn)
        # Sort BY the server-side rank, then slice by COUNT.
        #
        # Each family is filtered to its own members first because label_rank and
        # vote_rank are INDEPENDENT and either may be None: a concept with an
        # embedding but no member in the query's ANN top-25 has vote_rank None (the
        # common case, not an edge case), and a concept with members but no
        # embedding has label_rank None. Sorting the raw list by a nullable rank
        # column raises TypeError comparing None to int.
        #
        # Slicing by count rather than testing `rank > N` is deliberate too: it
        # cannot be broken by a future change to how dense the server's ranks are.
        if not skip_label_cosine:
            by_label = sorted((c for c in concepts if c["label_rank"] is not None),
                              key=lambda c: c["label_rank"])
            for c in by_label[:ENTRY_CONCEPT_LABEL_TOP]:
                _add("concept", c["concept_id"], "label-cosine", c["name"])
        # C is UNIONED with A, never a replacement for it -- measured A[:3] u C[:2]
        # beats either family alone.
        by_vote = sorted((c for c in concepts if c["vote_rank"] is not None),
                         key=lambda c: c["vote_rank"])
        for c in by_vote[:ENTRY_CONCEPT_VOTE_TOP]:
            _add("concept", c["concept_id"], "evidence-vote", c["name"])

    # N -- name anchor. ONLY the precise rungs. `description` and `memory-vote`
    # resolve an entity from CONTENT similarity, which is exactly the judgment this
    # crawl exists to route around rather than perform itself; and memory-vote in
    # particular re-derives the same evidence signal family C already carries, so
    # admitting it would double-count one measurement as two entry families.
    hits = kb.find_entities(q.text, embedding=label_vec, conn=conn)
    precise = [h for h in hits if h["via"] in ("name", "fuzzy-name")]
    for h in precise[:ENTRY_NAME_ANCHOR_TOP]:
        # find_entities returns `id` as a STRING
        # (KnowledgeBase._entity_profile_dict). The UUID() cast is required, not defensive: every
        # visited set and every bridge_frontier argument downstream is keyed by UUID,
        # and a str would silently never match.
        _add("entity", UUID(h["id"]), h["via"], h["name"])

    return seeds
