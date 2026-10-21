"""Precision controls on search(): scaffolding exclusion, lifecycle filtering, and
the confidence signal.

The kNN arm is dense — every embedded row has SOME cosine to any query — so it always
returns a full page and an off-corpus question is indistinguishable from a correct one.
Separately, entity/folder scaffolding competes for top-k on short queries, and retired
memories can take a large share of top-10 slots on project questions.

The fix for the first is a set-level WARNING, not a row filter: the per-row gold and
junk cosine distributions overlap, so filtering costs recall. See the CONFIDENCE_*
calibration note in knowledge_base.py.
"""

from kb_core.domain import NodePatch, NodeRecord
from kb_core.knowledge_base import (CONFIDENCE_STRONG, CONFIDENCE_WEAK,
                                    search_confidence)


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _blend(hot, other, sim):
    v = [0.0] * 1024
    v[hot] = sim
    v[other] = (1 - sim * sim) ** 0.5
    return v


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


# ---- no row filtering on similarity ---------------------------------------------

def test_weak_matches_are_returned_not_filtered(kb):
    """Deliberate: gold and off-corpus cosine distributions overlap per-row, so a
    filter deep enough to cut junk also cuts real hits. Recall is preserved and the
    caller is warned via confidence instead."""
    n = _note(kb, "weak", "alpha")
    kb.set_embedding(n.id, _blend(0, 1, 0.30), embedded_hash="h")
    res = kb.search("no lexical overlap whatsoever", embedding=_vec(0))
    assert [r.node.external_id for r in res] == ["weak"]
    assert search_confidence(res)["level"] == "none"   # returned, but flagged


def test_off_corpus_query_is_flagged_even_though_rows_come_back(kb):
    """The headline failure: rows always exist, so the WARNING is the fix."""
    for i in range(5):
        n = _note(kb, f"n{i}", f"meeting notes about the club {i}")
        kb.set_embedding(n.id, _blend(0, 1, 0.35), embedded_hash=f"h{i}")
    res = kb.search("quantum cryptography patent filing", embedding=_vec(0))
    assert res, "ranking still returns the closest rows"
    assert search_confidence(res)["level"] == "none", "but it must not look like an answer"


# ---- scaffolding ----------------------------------------------------------------

def test_entity_and_folder_stubs_do_not_compete_for_top_k(kb):
    """Stub nodes carry a non-empty content_tsv and were leaking into results."""
    _note(kb, "real", "globex shipping contract renewal")
    kb.upsert_entity("Globex")
    res = kb.search("globex")
    assert res, "the real note should match"
    assert {r.node.type for r in res} == {"note"}


def test_explicit_type_still_wins(kb):
    """The exclusion is a default, not a prohibition."""
    kb.upsert_entity("Globex")
    res = kb.search("globex", type="entity")
    assert [r.node.type for r in res] == ["entity"]


# ---- lifecycle ------------------------------------------------------------------

def test_dead_memories_are_included_by_default(kb):
    """Historical questions legitimately want retired notes; filtering them
    unconditionally costs recall on exactly those items."""
    n = _note(kb, "gone", "retired project alpha")
    kb.mark_dead(n.id, reason="superseded")
    assert [r.node.external_id for r in kb.search("retired project alpha")] == ["gone"]


def test_include_dead_false_drops_them(kb):
    live = _note(kb, "live", "project alpha status")
    dead = _note(kb, "dead", "project alpha status")
    kb.mark_dead(dead.id, reason="superseded")
    res = kb.search("project alpha status", include_dead=False)
    assert [r.node.external_id for r in res] == ["live"]
    assert live.id and dead.id      # both existed; only one came back


# ---- the confidence signal ------------------------------------------------------

def test_confidence_levels_track_the_best_similarity(kb):
    assert search_confidence([])["level"] == "none"

    n = _note(kb, "c1", "alpha")
    kb.set_embedding(n.id, _vec(0), embedded_hash="h")
    strong = kb.search("nothing lexical here", embedding=_vec(0))
    conf = search_confidence(strong)
    assert conf["level"] == "strong"
    assert conf["best_similarity"] >= CONFIDENCE_STRONG
    assert conf["n"] == 1


def test_confidence_is_keyword_when_no_vector_arm_ran(kb):
    """FTS-only is not 'low confidence' — a literal hit vouches for itself."""
    _note(kb, "k", "zanzibar retrospective")
    conf = search_confidence(kb.search("zanzibar"))
    assert conf["level"] == "keyword"
    assert conf["best_similarity"] is None


def test_scoped_results_are_not_judged_on_similarity(kb):
    """A hard facet answers the question; cosine to the phrasing is irrelevant.

    A facet query can have low cosine to its gold yet be exactly right:
    kind='troubleshooting log' answers a request for troubleshooting logs.
    """
    n = _note(kb, "log", "chased down the diff bug")
    kb.update(n.id, NodePatch(doc_kind="troubleshooting log"))
    kb.set_embedding(n.id, _blend(0, 1, 0.20), embedded_hash="h")
    res = kb.search("do I have any troubleshooting write-ups", embedding=_vec(0),
                    kind="troubleshooting log")
    assert [r.node.external_id for r in res] == ["log"]
    assert search_confidence(res)["level"] == "none"                # unscoped view
    assert search_confidence(res, scoped=True)["level"] == "scoped"  # honest view
    # calibration sanity, not a live re-check: see the CURRENT CALIBRATION note in
    # knowledge_base.py (instructed query vectors).
    assert CONFIDENCE_WEAK > 0.5


def test_confidence_pair_is_the_instructed_v1_derivation():
    # Derived for kb_core.embedding.QUERY_INSTRUCTION_VERSION "instructed-v1" by
    # evals/retrieval/shadow_confidence.py. A new instruction or
    # embedding model invalidates it: re-derive, never nudge.
    assert (CONFIDENCE_WEAK, CONFIDENCE_STRONG) == (0.512, 0.556)


# ---- entity_activity ordering ---------------------------------------------------

def test_dated_memories_lead_and_undated_follow(kb):
    """Undated notes carry a source timestamp that bulk file ops flattened into a
    couple of one-second blocks. Interleaving on that value does not order the
    timeline, it fabricates one — a declared March meeting date must not lose its place
    to a file whose mtime happens to be later."""
    from datetime import datetime, timezone
    ent = kb.upsert_entity("Acme")
    dated = _note(kb, "dated", "the march planning meeting")
    kb.update(dated.id, NodePatch(content="the march planning meeting"))
    with kb._pool.connection() as conn:
        conn.execute("UPDATE nodes SET event_at = %s WHERE id = %s",
                     (datetime(2025, 3, 1, tzinfo=timezone.utc), dated.id))
        # an undated note whose *source* timestamp is much later
        undated = _note(kb, "undated", "no declared date")
        conn.execute("UPDATE nodes SET event_at = NULL, source_created_at = %s "
                     "WHERE id = %s",
                     (datetime(2025, 9, 5, tzinfo=timezone.utc), undated.id))
    for n in (dated, undated):
        kb.link_entity(n.id, ent.node.id, "mentions", weight=1.0)
    rows = kb.entity_activity(ent.node.id)
    assert [n.external_id for n, _lt, _w in rows] == ["dated", "undated"]


def test_equal_timestamps_break_on_importance_not_path(kb):
    """Ties used to fall through to nodes.path — alphabetical, i.e. filesystem-walk
    order, which can bury the most important memory below trivial ones."""
    from datetime import datetime, timezone
    ent = kb.upsert_entity("Globex")
    same = datetime(2025, 3, 5, 9, 0, tzinfo=timezone.utc)
    a = _note(kb, "aaa_trivial", "alpha")     # sorts FIRST by path
    z = _note(kb, "zzz_important", "beta")    # sorts LAST by path
    with kb._pool.connection() as conn:
        conn.execute("UPDATE nodes SET event_at = NULL, source_created_at = %s "
                     "WHERE id = ANY(%s::uuid[])", (same, [a.id, z.id]))
    for n in (a, z):
        kb.link_entity(n.id, ent.node.id, "mentions", weight=1.0)
    kb.set_importance(z.id, 5.0)
    rows = kb.entity_activity(ent.node.id)
    assert [n.external_id for n, _lt, _w in rows] == ["zzz_important", "aaa_trivial"]


# ---- ANN-shaped kNN arm (migration 0037) ----------------------------------------

def test_chunk_hit_still_beats_a_weaker_whole_note_hit(kb):
    """The two ANN legs must max-reduce per note, reproducing the old greatest().

    A note whose BODY is unremarkable but which contains one highly relevant passage
    has to outrank a note that is mildly relevant throughout — that is the entire
    reason node_chunks exists, and the union rewrite must not lose it.
    """
    chunky = _note(kb, "chunky", "a " * 3000)
    middling = _note(kb, "middling", "b " * 3000)
    kb.set_embedding(chunky.id, _blend(0, 1, 0.10), embedded_hash="h1")   # poor overall
    kb.set_chunk_embeddings(chunky.id, [(0, 1500, _vec(0))])              # perfect passage
    kb.set_embedding(middling.id, _blend(0, 1, 0.60), embedded_hash="h2")  # decent overall
    res = kb.search("no lexical overlap at all", embedding=_vec(0))
    assert [r.node.external_id for r in res] == ["chunky", "middling"]
    assert res[0].similarity > 0.99      # scored by its best CHUNK, not its body


def test_a_note_matching_in_both_legs_appears_once(kb):
    """UNION ALL + GROUP BY: a note reachable via both its own vector and its chunks
    must not be duplicated, and must carry the better of the two scores."""
    n = _note(kb, "both", "c " * 3000)
    kb.set_embedding(n.id, _blend(0, 1, 0.70), embedded_hash="h")
    kb.set_chunk_embeddings(n.id, [(0, 1500, _vec(0))])
    res = kb.search("nothing lexical", embedding=_vec(0))
    assert [r.node.external_id for r in res] == ["both"]     # exactly one row
    assert res[0].similarity > 0.99                          # the chunk's score won


def test_filters_still_bind_the_chunk_leg_after_the_rewrite(kb):
    """The chunk leg's filters now apply AFTER its ANN limit, so this guards the
    thing that reordering could quietly break: a scoped search must never return a
    note outside the scope, however good its chunk is."""
    keep = _note(kb, "keep_c", "d " * 3000, tags=["keep"])
    drop = _note(kb, "drop_c", "e " * 3000, tags=["drop"])
    for n in (keep, drop):
        kb.set_embedding(n.id, _blend(0, 1, 0.20), embedded_hash="h")
    kb.set_chunk_embeddings(keep.id, [(0, 1500, _blend(0, 1, 0.80))])
    kb.set_chunk_embeddings(drop.id, [(0, 1500, _vec(0))])   # the BEST chunk overall
    res = kb.search("nothing lexical", embedding=_vec(0), tags=["keep"])
    assert [r.node.external_id for r in res] == ["keep_c"]


def test_knn_arm_is_deterministic_across_identical_calls(kb):
    """`id` is the final tiebreak so RRF gets stable ranks. Without it, equal
    (similarity, importance) rows permuted freely and eval reruns disagreed."""
    for i in range(6):
        n = _note(kb, f"tie{i}", "identical body text")
        kb.set_embedding(n.id, _blend(0, 1, 0.50), embedded_hash=f"h{i}")
    runs = {tuple(r.node.external_id for r in
                  kb.search("nothing lexical here", embedding=_vec(0)))
            for _ in range(5)}
    assert len(runs) == 1, f"kNN arm returned {len(runs)} different orderings"
