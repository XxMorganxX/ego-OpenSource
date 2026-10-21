from kb_core.domain import NodeRecord


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _set_entity_embedding(pool, entity_id, vec):
    literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
    with pool.connection() as conn:
        conn.execute("UPDATE entities SET embedding = %s::vector WHERE node_id = %s",
                     (literal, entity_id))


def test_exact_name_still_wins_and_is_tagged(kb):
    kb.upsert_entity("Vandelay")
    kb.upsert_entity("Vandelay Cloud")
    hits = kb.find_entities("Vandelay")
    assert hits[0]["name"] == "Vandelay"
    assert hits[0]["via"] == "name" and hits[0]["score"] == 1.0


def test_fuzzy_rung_catches_typos(kb):
    kb.upsert_entity("Vandelay")
    hits = kb.find_entities("Vandelaly")            # not a substring — ILIKE misses
    assert hits and hits[0]["name"] == "Vandelay"
    assert hits[0]["via"] == "fuzzy-name" and hits[0]["score"] > 0.25


def test_description_rung_resolves_semantically(kb, pool):
    ent = kb.upsert_entity("Acme", description="logistics startup")
    other = kb.upsert_entity("Beta Corp", description="a bakery chain")
    _set_entity_embedding(pool, ent.node.id, _vec(0))
    _set_entity_embedding(pool, other.node.id, _vec(1))
    hits = kb.find_entities("the startup building a route planner",
                            embedding=_vec(0))
    assert hits and hits[0]["name"] == "Acme"
    assert hits[0]["via"] == "description" and hits[0]["score"] > 0.99


def test_memory_vote_rung_resolves_through_evidence(kb):
    ent = kb.upsert_entity("Mystery Co")            # no entity embedding at all
    mem = kb.upsert(NodeRecord(source="agent", external_id="mv1", type="note",
                               content="notes about the secretive company",
                               slug="mv1"))
    kb.set_embedding(mem.id, _vec(2), embedded_hash="h")
    kb.link_entity(mem.id, ent.node.id, "mentions", weight=1.0)
    hits = kb.find_entities("zzz nothing matches this name", embedding=_vec(2))
    assert hits and hits[0]["name"] == "Mystery Co"
    assert hits[0]["via"] == "memory-vote" and hits[0]["score"] == 1.0


def test_dedupe_keeps_most_precise_rung(kb, pool):
    ent = kb.upsert_entity("Gamma", description="gamma things")
    _set_entity_embedding(pool, ent.node.id, _vec(3))
    # name-matches AND description-matches: the single row keeps via='name'
    hits = kb.find_entities("Gamma", embedding=_vec(3))
    assert [h["name"] for h in hits] == ["Gamma"]
    assert hits[0]["via"] == "name"


def test_no_embedding_means_lexical_rungs_only(kb):
    kb.upsert_entity("Delta")
    assert kb.find_entities("unrelated words", embedding=None) == []
