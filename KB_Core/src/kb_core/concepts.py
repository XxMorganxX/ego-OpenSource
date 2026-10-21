"""Pure helpers for the concept-consolidation pass: graph assembly, Leiden community
detection, membership scoring, and run-to-run concept matching. No DB or network —
independently testable (mirrors entities.py / embedding.py).

The consolidation graph is bipartite — memories <-> entity instances via mention links —
plus memory<->memory embedding-kNN edges. A community over that graph is a candidate
concept: it arrives with its memories AND its cast of entities attached. Entities connect
only through memories, so what binds a community is co-occurrence, not name similarity.
"""
from __future__ import annotations

import math

import igraph
import leidenalg

MEMORY = "memory"
ENTITY = "entity"


def mention_edge_weight(confidence: "float | None", support: int, *,
                        alpha: float = 1.0) -> float:
    """Weight of one memory--entity mention edge: extraction confidence damped by the
    entity's ubiquity. An entity mentioned in most memories carries little grouping signal
    (it glues unrelated memories together), so weight falls off as log2 of its
    distinct-memory support: 1 -> conf, 3 -> conf/2, 15 -> conf/4.

    `alpha` is the explicit mixing ratio against the kNN family, default 1.0 (no change).
    It exists because the entity:embedding weight split -- measured at roughly a quarter /
    three quarters -- was never chosen: it fell out of this damping curve, which was picked for
    hub control. Undamped the split would be roughly even. Without a coefficient here,
    "should entities matter more?" is not an answerable question.
    """
    conf = 0.5 if confidence is None else min(1.0, max(0.0, confidence))
    return alpha * conf / math.log2(1 + max(1, support))


def hub_capped_entities(mention_edges: "list[dict]", hub_share: "float | None", *,
                        coherence: "dict[str, float] | None" = None,
                        baseline: "float | None" = None,
                        min_lift: float = 1.0,
                        min_support: int = 3) -> "set[str]":
    """Entity ids to treat as CLUSTERING stopwords — excluded from the graph Leiden
    partitions. Not a demotion: their links, pages and retrieval are untouched, and
    `attach_entities(capped_edges=...)` still puts them on the rosters of the concepts
    their memories formed. Pure, and shared by build_graph and run_once so the rule
    lives in exactly one place.

    TWO gates, and which one runs depends on whether the caller can measure coherence.

    **Coherence (preferred).** `coherence` maps entity id -> mean pairwise cosine among
    the memories mentioning it, and `baseline` is that same mean over all memory pairs
    in the corpus. An entity is a stopword when its memories are no more alike than two
    drawn at random — `coherence < baseline * min_lift`. This measures the property the
    frequency rule was only ever proxying for, it is self-calibrating (the baseline
    moves with the corpus, so no per-corpus retuning), and it uses the embedding space
    as an independent judge of the entity layer. Entities with no score (support 1 has
    no pair to measure) are kept: they are structurally inert anyway, and cutting one
    would only cost it a roster slot. `min_lift` demands a margin over baseline rather
    than a bare win, and `min_support` demands enough mentions for the mean to mean
    anything — at support 2 the "mean" is one cosine, and an entity that thin binds a
    single pair, so condemning it on one noisy sample buys nothing. Coherence decides,
    but only once recurrence has supplied enough of it to decide on.

    DEFAULTED OFF, opt-in via KB_CONSOLIDATE_MIN_COHERENCE_LIFT. Measured
    after a duplicate merge changed the inputs, the coherence gate FUSED a
    mid-sized community with unrelated material into a blob twice its size, where
    the frequency gate kept it clean. Suspected mechanism, not yet confirmed: cutting a
    hub removes edge weight, which lowers modularity's total-weight term and so lowers
    the EFFECTIVE resolution — bigger, fused communities. If that holds, any hub cap can
    cause the very welding it exists to prevent, and the gate needs a resolution
    correction rather than a threshold tweak. Do not re-enable by default until that is
    understood.

    The lift distribution is CONTINUOUS, not cleanly separated: entities near 1.0
    cluster tightly on either side of it — so `min_lift` is a
    judgement, not a natural boundary, and entities near it will flip between runs as
    the corpus shifts. It is a better-founded judgement than frequency (it measures the
    property rather than a proxy for it) but it is not a bright line, and a known-bad
    extraction artifact can survive right at 1.00.

    **Frequency (fallback).** Without coherence data, cap at more than `hub_share` of
    the corpus's distinct memories, floor `max(4, ...)` so tiny corpora stay intact.
    Frequency is a poor proxy and was measured getting both directions wrong: it cut a
    specific project entity (high support, lift well above 1 — specific AND
    recurrent, the ideal shape) while keeping a ubiquitous personal name (lower support,
    lift < 1 — whose memories are LESS alike than random pairs). Coherence ranked every
    measured entity correctly, with a clean gap around 1.0.

    Residual risk, accepted deliberately: a very frequent entity that clears the lift
    bar is now kept where frequency would have cut it. That is the specific-project case and
    keeping it is the point — if an entity's memories genuinely cohere, binding them is
    correct however often it appears.

    `support` is per-edge here but assumed constant per entity — concept_mention_edges
    computes it via count(*) OVER (PARTITION BY entity_id), so every row for a given
    entity carries the same value. If edges for one entity ever disagreed, the `>` test
    is applied edge-by-edge: any single over-cap edge is enough to drop the entity
    (conservative direction — under-capping, not over-capping, would need every edge to
    agree)."""
    if not mention_edges:
        return set()
    if coherence is not None and baseline:
        floor = baseline * min_lift
        return {str(e["entity_id"]) for e in mention_edges
                if e["support"] >= min_support
                and coherence.get(str(e["entity_id"]), floor) < floor}
    if hub_share is None:
        return set()
    n_mem = len({str(e["memory_id"]) for e in mention_edges})
    cap = max(4, hub_share * n_mem)
    return {str(e["entity_id"]) for e in mention_edges if e["support"] > cap}


def build_graph(mention_edges: "list[dict]", knn_edges: "list[dict]", *,
                hub_share: "float | None" = 0.10,
                hubs: "set[str] | None" = None,
                entity_alpha: float = 1.0) -> "igraph.Graph":
    """Assemble the weighted consolidation graph.

    mention_edges rows: {memory_id, entity_id, weight (confidence|None), support}.
    knn_edges rows:     {src, dst, sim[, weight]} memory pairs — deduped here canonical-
    ordered, keeping the max when a pair is seen from both sides; `weight` (age-decayed,
    from memory_knn_edges) is used when present, else `sim`.
    Vertices are node-id strings with a `kind` attr (memory|entity); edges carry `weight`.

    Hub cap: mention edges of entities identified by `hub_capped_entities`
    are dropped from THIS graph only — the entity itself, its links, and retrieval are
    untouched, and `attach_entities(capped_edges=...)` still gives them their concept
    rosters. `hub_share=None` disables it.

    `hubs` accepts an already-computed stopword set, so a caller that gates on coherence
    (which needs a DB read this pure module must not do) can decide membership itself and
    have this build the same graph. Pass the SAME set to `attach_entities` as
    `capped_edges`' source, or the entities cut here lose their rosters.

    `entity_alpha` scales every mention edge and leaves kNN edges untouched -- the explicit
    entity:embedding mixing ratio. Default 1.0 is a byte-for-byte no-op.
    """
    if hubs is None:
        hubs = hub_capped_entities(mention_edges, hub_share)
    if hubs:
        mention_edges = [e for e in mention_edges if str(e["entity_id"]) not in hubs]
    kinds: "dict[str, str]" = {}
    weights: "dict[tuple[str, str], float]" = {}
    for e in mention_edges:
        m, ent = str(e["memory_id"]), str(e["entity_id"])
        kinds[m] = MEMORY
        kinds[ent] = ENTITY
        weights[(m, ent)] = (mention_edge_weight(e["weight"], e["support"],
                                                 alpha=entity_alpha)
                             * float(e.get("age_factor", 1.0)))
    for e in knn_edges:
        a, b = sorted((str(e["src"]), str(e["dst"])))
        kinds.setdefault(a, MEMORY)
        kinds.setdefault(b, MEMORY)
        # Decayed weight when the row carries one (memory_knn_edges does); raw sim for
        # hand-built rows and pre-decay snapshots. Both directions of a mutual pair
        # carry the same GREATEST(f_src, f_dst), so the max-dedup is unaffected.
        weights[(a, b)] = max(weights.get((a, b), 0.0),
                              float(e.get("weight", e["sim"])))
    # CANONICAL ORDER, not arrival order. Leiden's local moving visits vertices by
    # index, so the vertex->index mapping is an input to the partition — `seed` pins the
    # RNG but not this. Built from dict insertion order, the mapping inherited whatever
    # order Postgres happened to return, and neither concept_mention_edges nor
    # memory_knn_edges has an ORDER BY (Postgres guarantees none without one). A plan
    # change, VACUUM or parallel scan was therefore enough to repartition the whole KB
    # and churn every concept id. Measured before this sort: shuffling the same rows
    # changed the community count — the same magnitude as the
    # knobs we tune on purpose, which also made A/B comparisons untrustworthy.
    names = sorted(kinds)
    index = {n: i for i, n in enumerate(names)}
    edges = sorted(weights)
    return igraph.Graph(
        n=len(names),
        edges=[(index[a], index[b]) for a, b in edges],
        vertex_attrs={"name": names, "kind": [kinds[n] for n in names]},
        edge_attrs={"weight": [weights[e] for e in edges]})


def detect_communities(graph: "igraph.Graph", *, resolution: float = 1.0,
                       min_memories: int = 3, split_cohesion: float = 0.5,
                       seed: int = 42) -> "list[dict]":
    """Leiden communities over the consolidation graph.

    Returns [{key, level, parent_key, members: [(node_id, kind, score)]}], members
    strongest-first. Communities under min_memories memory members are dropped (noise).
    Every level-0 community is re-clustered once on its induced subgraph; a child is
    emitted as a level-1 concept (the parent keeps its full membership) only when it
    clears min_memories AND split_cohesion — the fraction of the edge weight touching
    the child, inside the parent's subgraph, that stays internal to it. Sub-structure,
    not size, is what earns a split: a composite community splits however small, a
    uniform blob emits no children however big, and one loose fragment dies without
    taking a crisp sibling down with it. The sub-partition must have more than one
    cell, since a single cell is just the parent again.

    `seed` pins Leiden's RNG, but that ALONE does not make a re-run reproducible: the
    partition also depends on the vertex->index mapping, because local moving visits
    vertices by index. Reproducibility comes from `seed` plus `build_graph`'s canonical
    (name-sorted) vertex order — see the note there. Passing a graph built any other way
    forfeits it."""
    if graph.vcount() == 0 or graph.ecount() == 0:
        return []
    out: "list[dict]" = []
    part = leidenalg.find_partition(
        graph, leidenalg.RBConfigurationVertexPartition,
        weights="weight", resolution_parameter=resolution, seed=seed)
    for ci, vertices in enumerate(part):
        comm = _community(graph, vertices, key=f"c{ci}", level=0, parent_key=None,
                          min_memories=min_memories)
        if comm is None:
            continue
        out.append(comm)
        sub = graph.induced_subgraph(vertices)
        subpart = leidenalg.find_partition(
            sub, leidenalg.RBConfigurationVertexPartition,
            weights="weight", resolution_parameter=resolution, seed=seed)
        if len(subpart) < 2:
            continue
        for si, sv in enumerate(subpart):
            if _cell_cohesion(sub, sv) < split_cohesion:
                continue
            child = _community(sub, sv, key=f"c{ci}.{si}", level=1,
                               parent_key=comm["key"], min_memories=min_memories)
            if child is not None:
                out.append(child)
    return out


def _cell_cohesion(sub: "igraph.Graph", cell) -> float:
    """Fraction of the edge weight touching a candidate child that stays inside it,
    measured on the parent's induced subgraph. 1.0 for a cell touching no edges."""
    inside = set(cell)
    internal = cross = 0.0
    for e in sub.es:
        w = float(e["weight"])
        a, b = e.source in inside, e.target in inside
        if a and b:
            internal += w
        elif a or b:
            cross += w
    total = internal + cross
    return internal / total if total else 1.0


def _community(graph, vertices, *, key, level, parent_key, min_memories):
    scores = _member_scores(graph, vertices)
    members = sorted(
        ((graph.vs[v]["name"], graph.vs[v]["kind"], scores[v]) for v in vertices),
        key=lambda m: -m[2])
    if sum(1 for _, kind, _ in members if kind == MEMORY) < min_memories:
        return None
    return {"key": key, "level": level, "parent_key": parent_key, "members": members}


def _member_scores(graph, vertices) -> "dict[int, float]":
    """Soft membership: the fraction of a vertex's edge weight that stays inside its
    community (1.0 for an isolated vertex)."""
    inside = set(vertices)
    scores: "dict[int, float]" = {}
    for v in vertices:
        total = internal = 0.0
        for eid in graph.incident(v):
            edge = graph.es[eid]
            w = float(edge["weight"])
            other = edge.source if edge.target == v else edge.target
            total += w
            if other in inside:
                internal += w
        scores[v] = round(internal / total, 3) if total else 1.0
    return scores


def match_concepts(communities: "list[dict]", previous: "list[dict]", *,
                   min_overlap: float = 0.5) -> "dict[str, object]":
    """Carry concept ids across runs: greedy one-to-one match of new communities to the
    previous ACTIVE concepts by Jaccard overlap of memory-member id sets; returns
    {community_key: previous_concept_id} for pairs at or above min_overlap. Memories
    only — entity membership churns faster and shouldn't break identity."""
    new_sets = {c["key"]: {nid for nid, kind, _ in c["members"] if kind == MEMORY}
                for c in communities}
    scored: "list[tuple[float, str, object]]" = []
    for key, mems in new_sets.items():
        for p in previous:
            prev = {str(i) for i in p["memory_ids"]}
            union = len(mems | prev)
            j = len(mems & prev) / union if union else 0.0
            if j >= min_overlap:
                scored.append((j, key, p["id"]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    matched: "dict[str, object]" = {}
    used: set = set()
    for j, key, pid in scored:
        if key in matched or pid in used:
            continue
        matched[key] = pid
        used.add(pid)
    return matched


def attach_entities(graph: "igraph.Graph", communities: "list[dict]", *,
                    min_share: float = 0.25,
                    exclude: "frozenset | set" = frozenset(),
                    capped_edges: "list[dict]" = ()) -> "list[dict]":
    """Entity membership is DERIVED, not partitioned: after communities are final, an
    entity joins every community that attracts at least min_share of its total edge
    weight — so a person who appears across two projects is a member of both concepts,
    not just the partition side Leiden happened to assign them. The added membership's
    score is that share (the same inside-fraction semantics as _member_scores).
    Memberships the partition already granted are kept untouched. Entities named in
    `exclude` (cannot_link anchors) gain no extra homes — multi-assigning an anchor
    could reunite a pair a human split. Entities attach only through their memories
    (the graph is bipartite), so this never moves or duplicates a memory.

    `capped_edges` are the mention edges `build_graph`'s hub cap dropped (same row
    shape). The cap is a CLUSTERING exclusion, not a roster exclusion: a hub entity
    must not influence the partition, but the concepts its memories formed should still
    list it — otherwise the most connective entity in a KB belongs to no concept at
    all, is never shown to the labeler (`concept_member_view` reads entity MEMBERS),
    and so cannot reach the label text the concept's own embedding is built from.
    Because it is not a graph vertex, its pull is measured from these raw edges instead
    of graph incidence; everything downstream — the share gate, `exclude`, the score
    semantics — is identical, so a diffuse stopword still earns no home."""
    if not communities or graph.vcount() == 0:
        return communities
    home: "dict[str, set]" = {}
    for ci, c in enumerate(communities):
        for nid, _kind, _score in c["members"]:
            home.setdefault(nid, set()).add(ci)
    out = [dict(c, members=list(c["members"])) for c in communities]

    def grant(name: str, pull: "dict[int, float]", total: float) -> None:
        if total <= 0:
            return
        for ci in sorted(pull):
            share = pull[ci] / total
            if share < min_share or ci in home.get(name, ()):
                continue
            out[ci]["members"] = sorted(
                out[ci]["members"] + [(name, ENTITY, round(share, 3))],
                key=lambda m: -m[2])

    for v in graph.vs:
        name = v["name"]
        if v["kind"] != ENTITY or name in exclude:
            continue
        total = 0.0
        pull: "dict[int, float]" = {}
        for eid in graph.incident(v.index):
            e = graph.es[eid]
            o = e.source if e.target == v.index else e.target
            w = float(e["weight"])
            total += w
            for ci in home.get(graph.vs[o]["name"], ()):
                pull[ci] = pull.get(ci, 0.0) + w
        grant(name, pull, total)

    capped_pull: "dict[str, dict[int, float]]" = {}
    capped_total: "dict[str, float]" = {}
    for e in capped_edges:
        name = str(e["entity_id"])
        if name in exclude:
            continue
        w = (mention_edge_weight(e["weight"], e["support"])
             * float(e.get("age_factor", 1.0)))
        capped_total[name] = capped_total.get(name, 0.0) + w
        pull = capped_pull.setdefault(name, {})
        for ci in home.get(str(e["memory_id"]), ()):
            pull[ci] = pull.get(ci, 0.0) + w
    for name in sorted(capped_pull):
        grant(name, capped_pull[name], capped_total[name])
    return out


def resolve_separations(rules: "list[dict]") -> "list[dict]":
    """Compile concept-pair separation rules ("these two CONCEPTS are unrelated") into
    node-anchored cannot_link constraints for THIS run. rules rows: {note, left_members,
    right_members} with each side's memory-member node ids strongest-first. Anchors are
    re-picked from current membership every run, so a rule survives member churn: each
    side anchors on its strongest memory member exclusive to it, falling back to its
    strongest member outright when one side is nested inside the other (a nested child
    has no exclusive members). A side with no members (retired/emptied concept) or a
    pair whose anchors coincide (identical membership) is inert this run."""
    out: "list[dict]" = []
    for rule in rules:
        left = [str(n) for n in rule["left_members"]]
        right = [str(n) for n in rule["right_members"]]
        l_set, r_set = set(left), set(right)
        a = next((n for n in left if n not in r_set), left[0] if left else None)
        b = next((n for n in right if n not in l_set), right[0] if right else None)
        if a is None or b is None or a == b:
            continue
        out.append({"kind": "cannot_link", "left_id": a, "right_id": b,
                    "note": rule.get("note")})
    return out


def enforce_constraints(graph: "igraph.Graph", communities: "list[dict]",
                        constraints: "list[dict]", *, resolution: float = 1.0,
                        min_memories: int = 3, max_escalations: int = 4) -> "list[dict]":
    """Apply human must_link / cannot_link constraints (node-id anchor pairs; entities
    or memories) to a detected community list. must_link runs first: the two communities
    containing the anchors are unioned. cannot_link then splits any community containing
    both anchors: re-cluster its induced subgraph at doubling resolution until the
    anchors separate, then coalesce the separating partition into exactly TWO sides
    around the anchors (whole cells stay together — a split never shatters a community
    into identity-losing fragments); if Leiden never separates them, a greedy seeded
    bipartition around the anchors forces it. Constraints whose anchors are absent (or
    already satisfied) are inert. Split parts keep the original's level/parent_key —
    they are siblings, not children.

    Splitting a community orphans any level-1 children detected under it, so a fixup
    pass re-parents each child to the split part holding most of its memory members —
    and drops a child whose memory set near-matches its new part's (Jaccard >= 0.75:
    the part supersedes it; identity still carries via memory-Jaccard matching).

    Interaction semantics (deliberate, documented limits): must_link runs before
    cannot_link and joint satisfaction is NOT attempted — a cannot_link split may
    re-separate a must_linked pair that shares a community with the cut. Dismissal
    matching decays as a cluster grows (tombstone member sets are frozen), so a
    long-dismissed, still-growing cluster can eventually resurface under a new id.
    """
    comms = [dict(c) for c in communities]

    def _home(node_id: str) -> "int | None":
        for i, c in enumerate(comms):
            if any(m[0] == node_id for m in c["members"]):
                return i
        return None

    for con in constraints:
        if con["kind"] != "must_link":
            continue
        i, j = _home(str(con["left_id"])), _home(str(con["right_id"]))
        if i is None or j is None or i == j:
            continue
        a, b = comms[i], comms[j]
        keep = a if a["level"] <= b["level"] else b
        vertices = [graph.vs.find(name=m[0]).index
                    for c in (a, b) for m in c["members"]]
        merged = _community(graph, vertices, key=keep["key"], level=keep["level"],
                            parent_key=keep["parent_key"], min_memories=1)
        comms[i] = merged
        comms.pop(j)

    cannot = [c for c in constraints if c["kind"] == "cannot_link"]
    out: "list[dict]" = []
    split: "dict[str, list[dict]]" = {}
    for c in comms:
        parts = _split_cannot(graph, c, cannot, resolution=resolution,
                              min_memories=min_memories,
                              max_escalations=max_escalations)
        if len(parts) != 1 or parts[0] is not c:
            split[c["key"]] = parts
        out.extend(parts)
    return _fix_children_of_split(out, split)


def _fix_children_of_split(out: "list[dict]", split: "dict[str, list[dict]]"):
    """Re-home level-1 children whose parent community was split away: each child moves
    under the split part holding most of its memory members; a child whose memory set
    near-matches its new part's (Jaccard >= 0.75) is dropped as a twin — the part
    supersedes it, and identity still carries to the part via memory-Jaccard matching.
    Exact equality is too strict here: membership drifts a member or two between runs,
    and a surviving near-twin steals the identity match from its own part, leaving the
    part to persist as a duplicate concept. 0.75 stays well above a genuine sub-cluster
    (a half-size child scores 0.5)."""
    if not split:
        return out
    def mems(c):
        return {n for n, kind, _ in c["members"] if kind == MEMORY}
    kept: "list[dict]" = []
    for c in out:
        parts = split.get(c.get("parent_key") or "")
        if parts is None:
            kept.append(c)
            continue
        if not parts:              # parent split to nothing (all pieces under
            continue               # min_memories) — its children die with it
        child_mems = mems(c)
        best = max(parts, key=lambda p: len(child_mems & mems(p)))
        union = len(child_mems | mems(best))
        if union and len(child_mems & mems(best)) / union >= 0.75:
            continue
        kept.append({**c, "parent_key": best["key"]})
    return kept


def _split_cannot(graph, community, cannot, *, resolution, min_memories,
                  max_escalations, _suffix=0):
    ids = {m[0] for m in community["members"]}
    pair = next((c for c in cannot
                 if str(c["left_id"]) in ids and str(c["right_id"]) in ids), None)
    if pair is None:
        return [community]
    vertices = [graph.vs.find(name=i).index for i in sorted(ids)]
    va = graph.vs.find(name=str(pair["left_id"])).index
    vb = graph.vs.find(name=str(pair["right_id"])).index
    parts: "list[list[int]] | None" = None
    sub = graph.induced_subgraph(vertices)
    # induced_subgraph sorts vertices by original index, so mapping must go through
    # names, never positions.
    by_name = {v["name"]: v.index for v in graph.vs}
    la = next(v.index for v in sub.vs if v["name"] == str(pair["left_id"]))
    lb = next(v.index for v in sub.vs if v["name"] == str(pair["right_id"]))
    for k in range(1, max_escalations + 1):
        p = leidenalg.find_partition(sub, leidenalg.RBConfigurationVertexPartition,
                                     weights="weight",
                                     resolution_parameter=resolution * (2 ** k), seed=42)
        membership = p.membership
        if membership[la] != membership[lb]:
            cells: "dict[int, list[int]]" = {}
            for li, gi in enumerate(membership):
                cells.setdefault(gi, []).append(li)
            # Coalesce the fine partition into exactly TWO sides around the anchors —
            # adopting every cell as its own part would shatter a large community into
            # fragments too small to carry its identity (memory-Jaccard match) forward.
            side_a, side_b = _coalesce_cells(sub, list(cells.values()), la, lb)
            parts = [[by_name[sub.vs[li]["name"]] for li in side]
                     for side in (side_a, side_b)]
            break
    if parts is None:
        side_a, side_b = _seeded_bipartition(graph, vertices, va, vb)
        parts = [side_a, side_b]
    result: "list[dict]" = []
    for n, part in enumerate(parts):
        piece = _community(graph, part, key=f"{community['key']}.x{_suffix + n}",
                           level=community["level"],
                           parent_key=community["parent_key"],
                           min_memories=min_memories)
        if piece is None:
            continue
        result.extend(_split_cannot(graph, piece, cannot, resolution=resolution,
                                    min_memories=min_memories,
                                    max_escalations=max_escalations,
                                    _suffix=_suffix + len(parts)))
    return result


def _coalesce_cells(graph, cells, seed_a: int, seed_b: int):
    """Merge a separating partition's cells into two sides anchored at seed_a/seed_b:
    greedily absorb the unassigned cell with the strongest total edge weight to either
    assigned side (ties -> side a, then lowest cell index). Whole cells move together,
    so the two-way cut respects the escalated partition's community structure instead
    of degrading to a vertex-level propagation. A cell attached to neither side (0.0
    both ways) falls to side a — deterministic, and irrelevant to anchor separation."""
    label: "dict[int, int]" = {}
    vert_cell: "dict[int, int]" = {}
    for ci, cell in enumerate(cells):
        for v in cell:
            vert_cell[v] = ci
        if seed_a in cell:
            label[ci] = 0
        if seed_b in cell:
            label[ci] = 1
    pending = {ci for ci in range(len(cells)) if ci not in label}
    while pending:
        att = {ci: [0.0, 0.0] for ci in pending}
        for e in graph.es:
            ca, cb = vert_cell[e.source], vert_cell[e.target]
            if ca == cb:
                continue
            w = float(e["weight"])
            if ca in att and cb in label:
                att[ca][label[cb]] += w
            if cb in att and ca in label:
                att[cb][label[ca]] += w
        best = max(pending, key=lambda ci: (max(att[ci]), -ci))
        label[best] = 0 if att[best][0] >= att[best][1] else 1
        pending.remove(best)
    side_a: "list[int]" = []
    side_b: "list[int]" = []
    for ci, cell in enumerate(cells):
        (side_a if label[ci] == 0 else side_b).extend(cell)
    return side_a, side_b


def _seeded_bipartition(graph, vertices, seed_a: int, seed_b: int):
    """Force-split a vertex set around two anchors by greedy label propagation:
    repeatedly assign the unassigned vertex with the strongest attachment to either
    side (ties -> side a). Deterministic; used only when Leiden escalation cannot
    separate a cannot_link pair. Absorbed influence is degree-normalized so a hub
    that glues two groups exerts thin per-neighbor pull and cannot drag whole
    groups across."""
    label: "dict[int, int]" = {seed_a: 0, seed_b: 1}
    strength: "dict[int, list[float]]" = {v: [0.0, 0.0] for v in vertices
                                          if v not in label}

    def absorb(v: int) -> None:
        deg = max(1, graph.degree(v))
        for eid in graph.incident(v):
            e = graph.es[eid]
            o = e.source if e.target == v else e.target
            if o in strength:
                strength[o][label[v]] += float(e["weight"]) / deg

    absorb(seed_a)
    absorb(seed_b)
    while strength:
        v = max(strength, key=lambda x: (max(strength[x]), -x))
        s = strength.pop(v)
        label[v] = 0 if s[0] >= s[1] else 1
        absorb(v)
    return ([v for v, s in label.items() if s == 0],
            [v for v, s in label.items() if s == 1])
