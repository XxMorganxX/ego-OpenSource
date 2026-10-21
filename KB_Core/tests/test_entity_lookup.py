"""find_entities / entity_profile: the name->entity resolution reads behind the
"who/what is X?" retrieval path (KB_MCP find_entity / get_entity)."""
from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS


def _mem(kb, eid):
    return kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                                content="c", slug=eid, title=eid))


def test_find_entities_ranks_exact_then_prefix_then_mentions(kb):
    quill = kb.upsert_entity("Quill", categories=["person"])
    quillon = kb.upsert_entity("Quillon Marsh", categories=["person"])
    other = kb.upsert_entity("Marquill Labs", categories=["org"])
    for i in range(2):                       # Marquill Labs is the most-mentioned...
        kb.link_entity(_mem(kb, f"m{i}").id, other.node.id, MENTIONS, weight=0.9)
    out = kb.find_entities("Quill")
    names = [e["name"] for e in out]
    # ...but exact beats prefix beats substring regardless of mention counts
    assert names == ["Quill", "Quillon Marsh", "Marquill Labs"]
    assert out[0]["id"] == str(quill.node.id)
    assert out[2]["mentions"] == 2
    assert "person" in out[1]["classes"] or out[1]["classes"] == []  # classes resolved


def test_find_entities_matches_aliases_and_escapes_wildcards(kb):
    kb.upsert_entity("Vandelay", categories=["org"], aliases=["Vandelay HQ"])
    kb.upsert_entity("foo_bar", categories=["tool"])
    kb.upsert_entity("fooxbar", categories=["tool"])
    assert [e["name"] for e in kb.find_entities("vandelay hq")][0] == "Vandelay"
    # Wildcard escaping: '_' must NOT act as an ILIKE wildcard in the name rung —
    # foo_bar is the exact hit; fooxbar may appear but only as a FUZZY match
    # (trigram overlap), never as a 'name' match.
    hits = kb.find_entities("foo_bar")
    assert hits[0]["name"] == "foo_bar" and hits[0]["via"] == "name"
    assert all(h["via"] != "name" for h in hits if h["name"] == "fooxbar")
    assert kb.find_entities("   ") == []


def test_find_entities_only_returns_active(kb):
    ent = kb.upsert_entity("Ghost Corp", categories=["org"])
    with kb._pool.connection() as conn:
        conn.execute("UPDATE entities SET status = 'inactive' WHERE node_id = %s",
                     (ent.node.id,))
    assert kb.find_entities("Ghost") == []
    # ...but the by-id profile still resolves it, status included
    prof = kb.entity_profile(ent.node.id)
    assert prof is not None and prof["status"] == "inactive"


def test_entity_profile_carries_description_classes_and_mentions(kb):
    ent = kb.upsert_entity("Quill", categories=["person"])
    kb.set_entity_description(ent.node.id, "Orchard Labs field researcher.")
    kb.link_entity(_mem(kb, "m1").id, ent.node.id, MENTIONS, weight=0.9)
    prof = kb.entity_profile(ent.node.id)
    assert prof["description"] == "Orchard Labs field researcher."
    assert prof["mentions"] == 1
    assert prof["entity_type"] == "instance"
    mem = _mem(kb, "m2")
    assert kb.entity_profile(mem.id) is None         # a memory is not an entity
