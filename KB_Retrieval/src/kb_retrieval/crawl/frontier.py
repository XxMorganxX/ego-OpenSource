"""Frontier construction: MENUS only, never a final rank.

Every function here returns candidates in a STRUCTURAL order -- the dense
server-side `ordinal` concept_frontier and bridge_frontier already computed
(concept_members.score DESC, or bridge cosine DESC, each with a deterministic
tiebreak). That order exists ONLY to decide what a judge sees, and in what sequence,
when a hop's candidates are truncated to FRONTIER_OFFERED. It NEVER reaches rank.py:
`menu_score` is carried on a FrontierItem for a judge's rendering and for the trace,
and rank.assemble() does not accept it.

Consuming the primitives' `ordinal` rather than re-sorting on the raw float is the
binding house rule, and the difference is real rather than ceremonial:
concept_members.score is saturated (most rows at or near 1.0), so a Python sort on it
would leave large tie groups ordered by list-arrival order.

RENDER METADATA (title / doc_kind / event_at) is fetched here by node_meta(),
UNCONDITIONALLY -- never gated on judge.uses_llm, never gated on q.embedding. An LLM
judge renders each row as "[i] title . doc_kind . sim=... . via=... .
event_at|undated" and needs all three; the deterministic judge reads none
of them. Fetching them only in the LLM mode would make the frontier's CONTENTS
differ by mode, which falsifies the design's "the deterministic crawl is the same
pipeline with decide() replaced by a rule" -- the claim the whole fail-soft design
rests on. tests/test_crawl_frontier.py pins the two modes byte-identical. The cost
is one indexed `id = ANY(...)` over at most FRONTIER_OFFERED (40) ids per hop.

hydrate() is NOT usable for this: it is reserved for the FINAL top-k by its own
contract, frontier rows are usually not the final top-k, and it pays for ts_headline
snippet generation nothing on this path reads. node_meta is the thin metadata-only
primitive that gap needed.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

FRONTIER_OFFERED = 40      # sized against an LLM judge's latency for this prompt shape
FRONTIER_ADMITTED = 12     # per hop
BRIDGE_FANOUT = 8          # per-entity mention fan-out, capped INSIDE bridge_frontier.
                           # Sits near instance p90 support, so it bites on a
                           # handful of hub entities. The design
                           # calls it untested -- a {8, 16} sweep is what settles it.


@dataclass(frozen=True)
class FrontierItem:
    """One row offered to a HopJudge.

    `idx` is this item's position in the OFFERED list -- what HopDecision.admit
    indexes into. A judge never emits a node id (prompt-injection containment): the
    worst a hostile note can achieve is getting ITSELF admitted,
    and it still has to survive a four-way fusion to reach a slot.

    `parents` is EVERY concept/entity that reached this node on this hop, not just
    the first. L_corrob counts DISTINCT bridge parents, so collapsing a node's
    second parent away would pin every corroboration count at 1 and turn L_corrob
    into a constant -- a fourth vote measuring nothing.

    `menu_score` is concept_members.score or the bridge cosine. ADMISSION AND
    DISPLAY ONLY. `similarity` is similarity_for's max(whole-note, best-chunk) --
    the one ruler, the same one that later enters ranking.

    `title` / `doc_kind` / `event_at` are RENDER metadata from node_meta: what
    prompts.render_frontier prints, and (for `title`) what strategy.py digests into
    the already-admitted summary a hop-2 prompt carries. Defaulted so every
    positional construction site and every test fake keeps working, and populated in
    BOTH judge modes (see this module's docstring). Any of the three may legitimately
    be None: in practice many notes have no event_at, and doc_kind is None until the
    doc_kind enrichment pass has run.
    """
    idx: int
    node_id: UUID
    family: str                 # "concept" | "bridge"
    parents: "tuple[UUID, ...]"
    menu_score: float
    similarity: "float | None"
    title: "str | None" = None
    doc_kind: "str | None" = None
    event_at: "datetime | None" = None


Frontier = "list[FrontierItem]"


def _offer(kb, q, conn, rows, *, family, node_key, parent_key, score_key,
           visited_nodes, visited_pairs) -> "list[FrontierItem]":
    """Collapse ordinal-ordered candidate rows into the offered frontier.

    ONE item per node -- a judge must not see the same title at two indices -- but
    ALL parents that reached it are kept on that item (see FrontierItem.parents).

    A node already in `visited_nodes` is skipped entirely. strategy.py SEEDS that
    set with the flat pool, so a flat row is never offered: L_admit is
    crawl-rows-only by design, and letting a flat row also collect an
    admission vote would let judgment promote a row the query already ranked.

    Nothing is marked visited here -- see _mark_offered. Marking before the
    FRONTIER_OFFERED cut would permanently burn candidates no judge ever saw, and
    deny a later hop a legitimate second route to the same memory.
    """
    order: "list[UUID]" = []
    seen: "dict[UUID, dict]" = {}
    for r in rows:
        node, parent = r[node_key], r[parent_key]
        if node in visited_nodes or (parent, node, family) in visited_pairs:
            continue
        if node not in seen:
            seen[node] = {"node": node, "parents": [parent],
                          "menu_score": float(r[score_key])}
            order.append(node)
        elif parent not in seen[node]["parents"]:
            seen[node]["parents"].append(parent)

    offered = [seen[n] for n in order[:FRONTIER_OFFERED]]
    ids = [o["node"] for o in offered]
    # ONE ruler for both families. bridge_frontier returns its own whole-note cosine,
    # but that is the MENU signal; the similarity a judge is shown is always
    # similarity_for's max(whole-note, best-chunk), so concept-family and
    # bridge-family rows are directly comparable in one rendering. Fetched here
    # rather than only in strategy.py because the design's claim is "the same pipeline
    # with decide() replaced by a rule" -- a frontier whose contents differed by mode
    # would falsify it.
    sim = (kb.similarity_for(ids, q.embedding, conn=conn)
           if ids and q.embedding is not None else {})
    # UNCONDITIONAL, unlike `sim` one line above: there is nothing to compare a row
    # against without a query vector, but a row's title does not depend on the query
    # at all. Gating this on q.embedding (or on judge.uses_llm) is the exact edit
    # test_offer_fetches_render_metadata_even_with_no_query_embedding exists to fail.
    meta = kb.node_meta(ids, conn=conn) if ids else {}
    out: "list[FrontierItem]" = []
    for i, o in enumerate(offered):
        m = meta.get(o["node"]) or {}
        out.append(FrontierItem(
            idx=i, node_id=o["node"], family=family,
            parents=tuple(o["parents"]), menu_score=o["menu_score"],
            similarity=sim.get(o["node"]), title=m.get("title"),
            doc_kind=m.get("doc_kind"), event_at=m.get("event_at")))
    return out


def _mark_offered(items, family, visited_nodes, visited_pairs) -> None:
    """Mark exactly what a judge was shown -- no more.

    `visited_pairs` is spec-mandated ("so two families cannot double-count one edge
    into L_corrob") but is REDUNDANT TODAY, stated plainly rather than implied:
    `visited_nodes` already blocks any node that was offered, so no edge can be seen
    twice, and concept ids and entity node ids are disjoint uuid spaces so no
    cross-family triple can collide. It is kept because it is the guard that
    survives if a future hop relaxes visited_nodes to let a node accumulate
    corroboration from a new parent -- adding that hop later must not silently
    double-count today's edges.
    """
    for it in items:
        visited_nodes.add(it.node_id)
        for parent in it.parents:
            visited_pairs.add((parent, it.node_id, family))


def hop1_frontier(kb, concept_ids, q, *, conn, visited_nodes, visited_pairs):
    """Concept-membership expansion. Returns `(frontier, harvested_bridge_ids)`.

    ENTITY members are never offered to a judge -- an entity is not a memory. They
    are harvested, in the primitive's own ordinal order, as hop 2's bridge seeds.
    """
    if not concept_ids:
        return [], []
    out = kb.concept_frontier(
        concept_ids, under=q.under, source=q.source, type=q.type, tags=q.tags,
        kind=q.kind, since=q.since, until=q.until, include_dead=q.include_dead,
        conn=conn)
    members = sorted(out["memories"], key=lambda m: m["ordinal"])
    entities = sorted(out["entities"], key=lambda e: e["ordinal"])

    bridges: "list[UUID]" = []
    for e in entities:
        if e["entity_id"] not in bridges:
            bridges.append(e["entity_id"])

    items = _offer(kb, q, conn, members, family="concept", node_key="id",
                   parent_key="concept_id", score_key="cm_score",
                   visited_nodes=visited_nodes, visited_pairs=visited_pairs)
    _mark_offered(items, "concept", visited_nodes, visited_pairs)
    return items, bridges


def hop2_frontier(kb, entity_ids, q, *, conn, visited_nodes, visited_pairs,
                  visited_bridges, fanout: int = BRIDGE_FANOUT):
    """Bridge (entity-mention) expansion. Returns `(frontier, bridge_capped)`.

    `bridge_capped` discloses, for every entity this hop expanded, its total mention
    support and how many memories were actually taken -- the design: nothing is
    refused, it is capped and SHOWN.

    `fanout` defaults to the module constant but is a caller-supplied parameter
    (for the design's untested {8, 16} sweep) so a benchmark can vary the
    per-entity hub cap without a code edit.
    """
    entity_ids = [e for e in entity_ids if e not in visited_bridges]
    if not entity_ids or q.embedding is None:
        # bridge_frontier orders by cosine, so with no query vector there is nothing
        # to order by. Returning early is not a silent skip: strategy.py has already
        # recorded degraded["embedder"].
        return [], []
    rows = kb.bridge_frontier(
        entity_ids, q.embedding, fanout=fanout, under=q.under,
        source=q.source, type=q.type, tags=q.tags, kind=q.kind, since=q.since,
        until=q.until, include_dead=q.include_dead, conn=conn)
    rows = sorted(rows, key=lambda r: r["ordinal"])

    # Name and support come back ON the rows, so the common case costs no extra
    # round trip. Only an entity whose every mention was filtered out has no row to
    # read them from -- that one falls back to entity_profile(), which is exactly why
    # that primitive needed a conn= parameter: without it this call would read
    # OUTSIDE the query's pinned snapshot. entity_profile's `mentions` is the same
    # count(DISTINCT memory_id) that bridge_frontier returns as `entity_support`, so
    # the two sources cannot disagree.
    known: "dict[UUID, tuple]" = {}
    taken: "dict[UUID, int]" = {}
    for r in rows:
        eid = r["via_entity_id"]
        known.setdefault(eid, (r["via_entity_name"], r["entity_support"]))
        taken[eid] = taken.get(eid, 0) + 1

    capped = []
    for eid in entity_ids:
        visited_bridges.add(eid)
        if eid in known:
            name, support = known[eid]
        else:
            profile = kb.entity_profile(eid, conn=conn)
            name = profile["name"] if profile else str(eid)
            support = profile["mentions"] if profile else 0
        capped.append({"name": name, "support": support,
                       "taken": taken.get(eid, 0), "of": support})

    items = _offer(kb, q, conn, rows, family="bridge", node_key="memory_id",
                   parent_key="via_entity_id", score_key="similarity",
                   visited_nodes=visited_nodes, visited_pairs=visited_pairs)
    _mark_offered(items, "bridge", visited_nodes, visited_pairs)
    return items, capped
