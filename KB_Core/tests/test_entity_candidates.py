from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


def _link_weight(kb, memory_id, entity_id, link_type):
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT weight FROM entity_links WHERE memory_id = %s AND entity_id = %s "
            "AND link_type = %s", (memory_id, entity_id, link_type)).fetchone()
    return row["weight"]


def test_staging_drops_junk_instance_names(kb):
    m = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(m.id, "notes", "README.md")          # filename
    kb.stage_entity_candidate(m.id, "people", "her")               # pronoun
    kb.stage_entity_candidate(m.id, "people", "Avery")            # real -> stages
    groups = kb.promotable_entities(threshold=1)
    assert [(g["entity_type"], g["raw_name"]) for g in groups] == [("people", "Avery")]


def test_staging_still_allows_class_votes(kb):
    # the junk gate is instance-only; class votes (entity_type='class') flow through untouched
    m = kb.upsert(_mem("m1"))
    kb.upsert_entity_class("people", active=False)
    kb.stage_entity_candidate(m.id, "class", "people")
    assert kb.activatable_classes(threshold=1)                     # the class vote staged


def test_staging_is_idempotent_per_memory(kb):
    m = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(m.id, "person", "Avery", confidence=0.8)
    kb.stage_entity_candidate(m.id, "person", "  avery ")     # same concept -> no duplicate
    groups = kb.promotable_entities(threshold=1)
    assert len(groups) == 1 and groups[0]["memory_count"] == 1


def test_promotable_respects_threshold(kb):
    for i in range(2):
        kb.stage_entity_candidate(kb.upsert(_mem(f"m{i}")).id, "person", "Avery")
    assert kb.promotable_entities(threshold=3) == []            # only 2 distinct memories
    kb.stage_entity_candidate(kb.upsert(_mem("m2")).id, "person", "Avery")
    groups = kb.promotable_entities(threshold=3)
    assert len(groups) == 1 and groups[0]["memory_count"] == 3


def _candidate_rows(kb, normalized_name):
    with kb._pool.connection() as conn:
        return conn.execute(
            "SELECT confidence FROM entity_candidates WHERE normalized_name = %s",
            (normalized_name,)).fetchall()


def test_untrusted_band_stages_but_never_creates_an_entity(kb):
    # The rubric's bottom band (0.3 -- "not a durable anchor") is RECORDED, not discarded:
    # it must stay visible/measurable, and still never mint an entity however often it recurs.
    for i in range(5):
        kb.stage_entity_candidate(kb.upsert(_mem(f"m{i}")).id, "concept", "build_pick_list",
                                  confidence=0.3)
    assert len(_candidate_rows(kb, "build_pick_list")) == 5      # staged, not dropped
    assert kb.promotable_entities(threshold=1) == []             # but never promotable


def test_one_trusted_mention_lifts_the_whole_group(kb):
    # The gate reads the group's BEST score, so a name judged durable in any memory promotes.
    kb.stage_entity_candidate(kb.upsert(_mem("m0")).id, "concept", "reconciliation",
                              confidence=0.3)
    kb.stage_entity_candidate(kb.upsert(_mem("m1")).id, "concept", "reconciliation",
                              confidence=0.9)
    groups = kb.promotable_entities(threshold=2)
    assert [g["normalized_name"] for g in groups] == ["reconciliation"]


def test_untrusted_mention_still_links_to_an_established_entity(kb):
    # Once an instance exists, later mentions link immediately regardless of confidence --
    # a single low score must not sever a real entity from a memory that mentions it.
    kb.stage_entity_candidate(kb.upsert(_mem("m0")).id, "person", "Avery", confidence=0.9)
    kb.promote_entity("person", "Avery")
    kb.stage_entity_candidate(kb.upsert(_mem("m1")).id, "person", "Avery", confidence=0.3)
    groups = kb.promotable_entities(threshold=99)                # far above recurrence
    assert [g["normalized_name"] for g in groups] == ["avery"]  # entity_exists leg, ungated


def test_absent_confidence_is_treated_as_trusted(kb):
    # Legacy rows and any writer that stages without a score must keep promoting as before.
    for i in range(2):
        kb.stage_entity_candidate(kb.upsert(_mem(f"m{i}")).id, "person", "Avery")
    assert [g["normalized_name"] for g in kb.promotable_entities(threshold=2)] == ["avery"]


def test_promote_creates_entity_and_backfills_edges(kb):
    mems = [kb.upsert(_mem(f"m{i}")) for i in range(3)]
    for m in mems:
        kb.stage_entity_candidate(m.id, "person", "Avery", confidence=0.7)
    ent = kb.promote_entity("person", "Avery")
    about = kb.memories_linked(ent.node.id, link_type=MENTIONS)
    assert {n.id for n in about} == {m.id for m in mems}       # every staged memory linked
    weights = [_link_weight(kb, m.id, ent.node.id, MENTIONS) for m in mems]
    assert weights == [0.7, 0.7, 0.7]                          # weight = candidate confidence
    assert kb.promotable_entities(threshold=1) == []           # candidates consumed


def test_promote_is_idempotent(kb):
    m = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(m.id, "person", "Avery", confidence=0.5)
    a = kb.promote_entity("person", "Avery")
    b = kb.promote_entity("person", "Avery")                  # re-promote -> no new node/link
    assert a.node.id == b.node.id
    assert len(kb.memories_linked(a.node.id, link_type=MENTIONS)) == 1


def test_promote_creates_canonical_instance_linked_to_its_category(kb):
    m = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(m.id, "people", "Avery", confidence=0.8)
    inst = kb.promote_entity("people", "Avery")
    # one canonical instance, placed flat (category is a link, not the tree location)
    assert inst.node.path == "entities.instances.Avery"
    assert inst.entity_type == "instance"
    cat = kb.get_entity_class("people")
    # category membership is an is_a link, not the parent_id
    assert cat is not None and [c.canonical_name for c in kb.classes_of(inst.node.id)] == ["people"]
    # the category node is materialized to link to, but NOT auto-activated — activation is
    # vote-driven (reconcile_classes), so promoting an instance alone doesn't offer the category
    assert cat.active is False and "people" not in kb.entity_class_names()
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Avery"]


def test_same_name_across_categories_is_one_instance_with_many_class_links(kb):
    # the core dedup win: "Bramblewood" seen as a project AND an org AND software is ONE node
    for i, cat in enumerate(["projects", "organizations", "software"]):
        kb.stage_entity_candidate(kb.upsert(_mem(f"p{i}")).id, cat, "Bramblewood")
        kb.promote_entity(cat, "Bramblewood")
    insts = [e for e in kb.list_entities(entity_type="instance")]
    assert [e.canonical_name for e in insts] == ["Bramblewood"]            # exactly one node
    assert sorted(c.canonical_name for c in kb.classes_of(insts[0].node.id)) == \
        ["organizations", "projects", "software"]                        # linked to all three
