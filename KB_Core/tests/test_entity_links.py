from kb_core.domain import NodeRecord
from kb_core.entities import BELONGS_TO, MENTIONS
from kb_core.ingest.llm import LLMEnricher, LLMPass, passes_for_phase
from kb_core.ingest.promotion import EntityPromotionWorker


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


def test_entity_links_table_accepts_a_link(kb, pool):
    m = kb.upsert(_mem("m1"))
    ent = kb.upsert_entity("Avery", categories=["person"])
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO entity_links (memory_id, entity_id, link_type, weight) "
            "VALUES (%s, %s, %s, %s)", (m.id, ent.node.id, MENTIONS, 0.5))
        row = conn.execute(
            "SELECT link_type, weight FROM entity_links WHERE memory_id = %s", (m.id,)
        ).fetchone()
    assert row["link_type"] == MENTIONS and row["weight"] == 0.5


def test_migration_backfill_moves_entity_edges_out_of_edges(kb, pool):
    # Seed `edges` as if pre-migration data existed: one entity link + one memory<->memory.
    m = kb.upsert(_mem("m1"))
    ent = kb.upsert_entity("Avery", categories=["person"])
    other = kb.upsert(_mem("m2"))
    with pool.connection() as conn:
        conn.execute("INSERT INTO edges (source_id, target_id, relation_type, weight) "
                     "VALUES (%s, %s, %s, %s)", (m.id, ent.node.id, BELONGS_TO, 0.9))
        conn.execute("INSERT INTO edges (source_id, target_id, relation_type) "
                     "VALUES (%s, %s, %s)", (m.id, other.id, "relates_to"))
        # Re-run the migration's data-movement SQL (the same statements as 0010).
        conn.execute(
            "INSERT INTO entity_links (memory_id, entity_id, link_type, weight, created_at, "
            "updated_at) SELECT source_id, target_id, relation_type, weight, created_at, "
            "updated_at FROM edges WHERE relation_type IN ('belongs_to', 'mentions') "
            "ON CONFLICT DO NOTHING")
        conn.execute("DELETE FROM edges WHERE relation_type IN ('belongs_to', 'mentions')")
        moved = conn.execute(
            "SELECT link_type, weight FROM entity_links WHERE memory_id = %s AND entity_id = %s",
            (m.id, ent.node.id)).fetchone()
        left = conn.execute("SELECT relation_type FROM edges WHERE source_id = %s", (m.id,)
                            ).fetchall()
    assert moved["link_type"] == BELONGS_TO and moved["weight"] == 0.9  # entity link relocated
    assert [r["relation_type"] for r in left] == ["relates_to"]        # memory<->memory stays


def test_link_entity_is_idempotent_and_updates_weight(kb):
    m = kb.upsert(_mem("m1"))
    ent = kb.upsert_entity("Avery", categories=["person"])
    kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.3)
    kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.7)   # same key -> update, no dup
    about = kb.memories_linked(ent.node.id, link_type=MENTIONS)
    assert [n.id for n in about] == [m.id]


def test_memories_linked_and_entities_of_filter_by_type(kb):
    m = kb.upsert(_mem("m1"))
    person = kb.upsert_entity("Avery", categories=["person"])
    cls = kb.upsert_entity_class("people")
    kb.link_entity(m.id, person.node.id, MENTIONS)
    kb.link_entity(m.id, cls.node.id, BELONGS_TO)
    # out-links from the memory, filtered
    assert {n.id for n in kb.entities_of(m.id)} == {person.node.id, cls.node.id}
    assert [n.id for n in kb.entities_of(m.id, link_type=BELONGS_TO)] == [cls.node.id]
    # in-links to an entity
    assert [n.id for n in kb.memories_linked(person.node.id, link_type=MENTIONS)] == [m.id]


def _rec(eid, content):
    return NodeRecord(source="agent", external_id=eid, type="note", content=content, slug=eid)


class _Scripted:
    """Returns a reply keyed by which pass (by user-prompt marker) is calling."""
    def __init__(self, by_marker):
        self._by_marker = by_marker

    def complete(self, system_prompt, user_prompt, *, schema=None):
        for marker, reply in self._by_marker.items():
            if marker in user_prompt:
                return reply
        return "{}"


def test_lifecycle_classes_first_then_instances_on_recrawl(kb):
    enrich_passes = [
        LLMPass("discover", "s", "DISCOVER {content}", target="entity_classes",
                schema={"type": "object"}, phase="enrich"),
        LLMPass("classify", "s", "CLASSIFY {entity_classes}", target="classify",
                schema={"type": "object"}, phase="enrich"),
    ]
    recrawl_passes = [
        LLMPass("extract", "s", "EXTRACT {content}", target="entities",
                schema={"type": "object"}, phase="recrawl"),
    ]
    all_passes = enrich_passes + recrawl_passes
    client = _Scripted({
        "DISCOVER": '{"classes": [{"name": "people", "status": "new"}]}',
        "CLASSIFY": '{"classes": ["people"]}',
        "EXTRACT": '{"entities": [{"category": "people", "name": "Avery", "confidence": 0.7}]}',
    })
    enrich = LLMEnricher(client, passes_for_phase(all_passes, "enrich"))
    recrawl = LLMEnricher(client, passes_for_phase(all_passes, "recrawl"))

    # First-time enrichment of 3 memories: each votes for "people"; NO instance staging here.
    mems = [kb.upsert(_rec(f"m{i}", "met some people")) for i in range(3)]
    for m in mems:
        enrich.enrich(kb.get(m.id), kb)
    assert kb.promotable_entities(threshold=1) == []        # no instances staged at first-time
    assert kb.entity_class_names() == []                    # class still inactive (needs sweep)

    # Settle sweep: "people" clears the vote floor (3) and, as the sole/leading class, the
    # dominance gate -> active + belongs_to backfill.
    EntityPromotionWorker(kb, threshold=2, class_floor=3).run()
    cls = kb.get_entity_class("people")
    assert cls.active is True
    assert {n.id for n in kb.memories_linked(cls.node.id, link_type=BELONGS_TO)} == \
           {m.id for m in mems}

    # Re-crawl two memories: instance "Avery" gets staged (>=2 distinct memories).
    for m in mems[:2]:
        recrawl.enrich(kb.get(m.id), kb)
    EntityPromotionWorker(kb, threshold=2, class_floor=3).run()
    inst = kb.instances_of("people")
    assert [e.canonical_name for e in inst] == ["Avery"]
    assert len(kb.memories_linked(inst[0].node.id, link_type=MENTIONS)) == 2
