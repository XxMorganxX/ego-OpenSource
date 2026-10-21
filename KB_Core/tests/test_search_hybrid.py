from datetime import datetime, timezone

from kb_core.domain import NodeRecord


def _vec(hot):
    """A 1024-dim unit vector with one hot dimension — cosine 1.0 to itself, 0.0 to
    any other; lets tests steer the kNN arm without a real embedding model."""
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _blend(hot, other, sim):
    """A unit vector whose cosine to _vec(hot) is exactly `sim`.

    Orthogonal `_vec` pairs score 0.0, which the KNN_MIN_SIM floor now (correctly)
    discards — so a test that needs a *weaker but genuine* neighbour has to ask for
    one explicitly instead of relying on the arm returning everything it saw.
    """
    v = [0.0] * 1024
    v[hot] = sim
    v[other] = (1 - sim * sim) ** 0.5
    return v


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def test_hybrid_surfaces_semantic_match_that_fts_misses(kb):
    hit = _note(kb, "pgvector_fix", "extension vector is not available after brew upgrade")
    other = _note(kb, "groceries", "buy apples and oat milk")
    kb.set_embedding(hit.id, _vec(0), embedded_hash="h1")
    # A weaker but real neighbour (0.7), not an orthogonal one — the floor discards
    # cosine-0.0 rows, which is the point of it.
    kb.set_embedding(other.id, _blend(0, 1, 0.7), embedded_hash="h2")
    query = "embedding database broken"          # matches neither content lexically
    assert kb.search(query) == []                # FTS alone finds nothing
    res = kb.search(query, embedding=_vec(0))
    assert [r.node.external_id for r in res] == ["pgvector_fix", "groceries"]
    assert res[0].similarity > 0.99              # pure kNN hit …
    assert res[0].score == 0                     # … with no FTS component
    assert res[0].rank > res[1].rank             # RRF ordering exposed via rank


def test_hybrid_fuses_both_arms_keyword_winner_first(kb):
    both = _note(kb, "kw", "the zanzibar deployment retrospective")
    near = _note(kb, "sem", "completely unrelated words about lunch")
    kb.set_embedding(both.id, _vec(2), embedded_hash="h1")
    kb.set_embedding(near.id, _vec(3), embedded_hash="h2")
    # "zanzibar" hits only `both`; the query vector sits on `near`'s dimension.
    res = kb.search("zanzibar", embedding=_vec(3))
    assert [r.node.external_id for r in res] == ["kw", "sem"]  # 1/61+1/62 beats 1/61
    assert res[0].score > 0                       # FTS arm contributed
    assert res[1].similarity > 0.99               # semantic-only runner-up
    assert res[1].score == 0


def test_search_time_window_bounds_event_time(kb):
    jan = _note(kb, "jan", "weekly sync meeting notes",
                event_at=datetime(2025, 1, 31, tzinfo=timezone.utc))
    march = _note(kb, "march", "weekly sync meeting notes again",
                  event_at=datetime(2025, 3, 10, tzinfo=timezone.utc))
    kb.set_embedding(jan.id, _vec(4), embedded_hash="h1")
    kb.set_embedding(march.id, _vec(5), embedded_hash="h2")
    got = kb.search("sync meeting", since=datetime(2025, 2, 1, tzinfo=timezone.utc))
    assert [r.node.external_id for r in got] == ["march"]
    # the window binds the kNN arm too — a perfect vector match outside it stays out
    got = kb.search("sync meeting", embedding=_vec(4),
                    since=datetime(2025, 2, 1, tzinfo=timezone.utc))
    assert [r.node.external_id for r in got] == ["march"]


def test_facet_filters_bind_the_knn_arm(kb):
    tagged = _note(kb, "tagged", "alpha content", tags=["keep"])
    excluded = _note(kb, "excluded", "beta content", tags=["drop"])
    # `excluded` is the BEST vector match and `tagged` only a decent one, so the tags
    # filter — not the similarity floor — has to be what removes it.
    kb.set_embedding(tagged.id, _blend(7, 6, 0.8), embedded_hash="h1")
    kb.set_embedding(excluded.id, _vec(7), embedded_hash="h2")
    res = kb.search("nonmatching words", embedding=_vec(7), tags=["keep"])
    assert [r.node.external_id for r in res] == ["tagged"]


def test_knn_arm_orders_by_importance_priority(kb):
    """Two memories with IDENTICAL embeddings: the important one must rank first —
    the kNN arm orders by cosine x the same importance/retrieval factor as FTS."""
    plain = _note(kb, "plain_sem", "unrelated alpha")
    vip = _note(kb, "vip_sem", "unrelated beta")
    kb.set_embedding(plain.id, _vec(8), embedded_hash="h1")
    kb.set_embedding(vip.id, _vec(8), embedded_hash="h2")
    kb.set_importance(vip.id, 5.0)
    res = kb.search("no lexical match here", embedding=_vec(8))
    assert [r.node.external_id for r in res] == ["vip_sem", "plain_sem"]
    assert res[0].similarity == res[1].similarity    # cosine tie — importance decided


def test_fts_or_fallback_rescues_keyword_bag_queries(kb):
    """AND-semantics matches nothing for a long keyword bag; the OR fallback must
    surface the best partial match instead of silently emptying the FTS arm."""
    hit = _note(kb, "shipping_call", "globex agreed to the shipping contract renewal")
    _note(kb, "other", "unrelated grocery list")
    res = kb.search("globex shipping pallet label size")
    assert [r.node.external_id for r in res] == ["shipping_call"]
    assert res[0].rank > 0
    # single-word and normally-matching queries never take the fallback path
    assert kb.search("nonexistentword") == []

LONG_REQUEST = "the globex shipping deal that we talked about with everyone last month"


def test_long_requests_keep_and_semantics_when_a_query_embedding_is_supplied(kb):
    """The flooding gate is unchanged in hybrid mode: >8 words stays AND-semantics, so
    no lexical match -> an empty FTS arm (long queries are the vector arm's job). The
    lexical match has no vector, so only an OR rewrite could surface it: the result
    stays empty, exactly as before."""
    _note(kb, "shipping_long", "globex agreed to the shipping contract renewal")
    assert len(LONG_REQUEST.split()) > 8
    assert kb.search(LONG_REQUEST, embedding=_vec(21)) == []
    # control: the same note IS reachable keyword-only (the OR rewrite fires there)
    assert [r.node.external_id for r in kb.search(LONG_REQUEST)] == ["shipping_long"]


def test_keyword_only_mode_lets_long_questions_fall_back_to_or(kb):
    """No embedder (or it is down): there is no vector arm to answer a natural-language
    question, so an AND-miss falls back to OR-joined terms up to a much higher cap,
    and ts_rank orders the partial matches."""
    _note(kb, "shipping_kw", "globex agreed to the shipping contract renewal")
    _note(kb, "other_kw", "unrelated grocery list")
    res = kb.search(LONG_REQUEST)
    assert [r.node.external_id for r in res] == ["shipping_kw"]
    # a question whose every term already co-occurs keeps plain AND semantics
    assert [r.node.external_id for r in kb.search("globex shipping contract")] == \
        ["shipping_kw"]
    # past the keyword-only cap the gate still closes
    from kb_core.knowledge_base import OR_FALLBACK_MAX_WORDS_KEYWORD_ONLY
    huge = " ".join(["globex"] + ["zzqx"] * OR_FALLBACK_MAX_WORDS_KEYWORD_ONLY)
    assert kb.search(huge) == []


def test_or_fallback_eligible_gate():
    from kb_core.knowledge_base import (OR_FALLBACK_MAX_WORDS,
                                        OR_FALLBACK_MAX_WORDS_KEYWORD_ONLY,
                                        or_fallback_eligible)
    nine = " ".join(["w"] * (OR_FALLBACK_MAX_WORDS + 1))
    assert or_fallback_eligible("a b", embedded=True)
    assert not or_fallback_eligible("single", embedded=True)
    assert not or_fallback_eligible("single", embedded=False)
    assert not or_fallback_eligible(nine, embedded=True)          # hybrid: unchanged
    assert or_fallback_eligible(nine, embedded=False)             # keyword-only: allowed
    assert not or_fallback_eligible(
        " ".join(["w"] * (OR_FALLBACK_MAX_WORDS_KEYWORD_ONLY + 1)), embedded=False)


def test_or_fallback_feeds_the_hybrid_fusion(kb):
    hit = _note(kb, "kwx", "globex agreed to the shipping contract renewal")
    sem = _note(kb, "semx", "different words entirely")
    kb.set_embedding(hit.id, _vec(10), embedded_hash="h1")
    kb.set_embedding(sem.id, _vec(11), embedded_hash="h2")
    # AND would match nothing -> pre-fix this returned only the vector arm
    res = kb.search("globex shipping pallet label size",
                    embedding=_vec(11))
    got = [r.node.external_id for r in res]
    assert "kwx" in got and "semx" in got     # both arms contributed
    assert res[[i for i, r in enumerate(res)
                if r.node.external_id == "kwx"][0]].score > 0


def test_knn_damping_keeps_similarity_dominant(kb):
    """sqrt-damped importance: a moderately-similar important note no longer beats
    a highly-similar plain one (raw factor 1.8x0.707>1 flipped; sqrt keeps order)."""
    import math
    plain = _note(kb, "plain_dmp", "unrelated one")
    vip = _note(kb, "vip_dmp", "unrelated two")
    kb.set_embedding(plain.id, _vec(12), embedded_hash="h1")
    half = [0.0] * 1024
    half[12] = half[13] = 1 / math.sqrt(2)     # cosine ~0.707 to the query
    kb.set_embedding(vip.id, half, embedded_hash="h2")
    kb.set_importance(vip.id, 0.8)     # modifiers add to base 1.0 -> importance 1.8
    res = kb.search("no lexical match here", embedding=_vec(12))
    # raw factor: 0.707 x 1.8 = 1.27 > 1.0 would flip; sqrt: 0.707 x 1.34 = 0.95 < 1.0
    assert [r.node.external_id for r in res] == ["plain_dmp", "vip_dmp"]


def test_fts_only_results_carry_no_similarity(kb):
    _note(kb, "plain", "a plain keyword note")
    res = kb.search("keyword")
    assert len(res) == 1 and res[0].similarity is None
    assert res[0].rank > 0 and "keyword" in res[0].snippet
