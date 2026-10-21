"""KnowledgeBase.reidentify: a loss-free in-place re-key that carries FK-referenced rows."""

from kb_core.domain import NodeRecord
from kb_core.identity import node_id


def _note(eid, slug, parent="A", content="c"):
    return NodeRecord(source="obsidian", external_id=eid, type="note",
                      content=content, slug=slug, parent_path=parent)


def test_reidentify_rekeys_in_place_preserving_placement_and_edges(kb, pool):
    a = kb.upsert(_note("A/x.md", "x", content="ca"))
    b = kb.upsert(_note("A/y.md", "y", content="cb"))
    with pool.connection() as c:
        c.execute("INSERT INTO edges (source_id, target_id, relation_type) "
                  "VALUES (%s, %s, 'relates_to')", (a.id, b.id))

    assert kb.reidentify([(a.id, "stable-id-1")]) == 1

    new_id = node_id("obsidian", "stable-id-1")
    assert kb.get(a.id) is None                              # old id retired
    moved = kb.get(new_id)
    assert moved is not None
    assert moved.external_id == "stable-id-1"
    assert moved.path == a.path and moved.content == "ca"    # placement + content preserved
    with pool.connection() as c:
        n = c.execute("SELECT count(*) n FROM edges WHERE source_id=%s AND target_id=%s",
                      (new_id, b.id)).fetchone()["n"]
    assert n == 1                                            # the edge followed the re-key


def test_reidentify_carries_entity_candidate_references(kb, pool):
    a = kb.upsert(_note("A/z.md", "z"))
    with pool.connection() as c:
        c.execute("INSERT INTO entity_candidates (memory_id, entity_type, normalized_name, raw_name) "
                  "VALUES (%s, 'topic', 'z', 'Z')", (a.id,))
    kb.reidentify([(a.id, "stable-z")])
    new_id = node_id("obsidian", "stable-z")
    with pool.connection() as c:
        n = c.execute("SELECT count(*) n FROM entity_candidates WHERE memory_id=%s",
                      (new_id,)).fetchone()["n"]
    assert n == 1                                            # candidate repointed, not orphaned


def test_reidentify_is_idempotent_when_id_unchanged(kb):
    a = kb.upsert(_note("keep.md", "keep", parent=""))
    assert kb.reidentify([(a.id, "keep.md")]) == 1           # same external_id -> same node id
    assert kb.get(a.id).external_id == "keep.md"             # still there, just refreshed


def test_reidentify_carries_node_processing(kb, pool):
    """node_processing rows must follow the node when it is re-keyed."""
    a = kb.upsert(_note("A/np.md", "np"))
    with pool.connection() as c:
        c.execute(
            "INSERT INTO node_processing (node_id, task, status, queued_at, updated_at) "
            "VALUES (%s, 'recrawl', 'pending', now(), now())",
            (a.id,),
        )
    kb.reidentify([(a.id, "stable-np")])
    new_id = node_id("obsidian", "stable-np")
    with pool.connection() as c:
        n = c.execute(
            "SELECT count(*) n FROM node_processing WHERE node_id=%s AND task='recrawl'",
            (new_id,),
        ).fetchone()["n"]
    assert n == 1                                            # row followed the re-key


def test_reidentify_carries_entity_class_links(kb, pool):
    """entity_class_links rows must follow the instance node when it is re-keyed."""
    instance = kb.upsert(_note("entities/inst.md", "inst", parent="entities"))
    cls = kb.upsert(_note("entities/cls.md", "cls", parent="entities"))
    with pool.connection() as c:
        c.execute(
            "INSERT INTO entity_class_links (instance_id, class_id) VALUES (%s, %s)",
            (instance.id, cls.id),
        )
    kb.reidentify([(instance.id, "stable-inst")])
    new_id = node_id("obsidian", "stable-inst")
    with pool.connection() as c:
        n = c.execute(
            "SELECT count(*) n FROM entity_class_links WHERE instance_id=%s",
            (new_id,),
        ).fetchone()["n"]
    assert n == 1                                            # link followed the re-key


def test_reidentify_carries_entity_merges_survivor(kb, pool):
    """entity_merges.survivor_id must follow when the survivor node is re-keyed."""
    survivor = kb.upsert(_note("entities/surv.md", "surv", parent="entities"))
    with pool.connection() as c:
        c.execute(
            "INSERT INTO entity_merges (survivor_id, source_name, kind) "
            "VALUES (%s, 'folded-entity', 'instance')",
            (survivor.id,),
        )
    kb.reidentify([(survivor.id, "stable-surv")])
    new_id = node_id("obsidian", "stable-surv")
    with pool.connection() as c:
        n = c.execute(
            "SELECT count(*) n FROM entity_merges WHERE survivor_id=%s",
            (new_id,),
        ).fetchone()["n"]
    assert n == 1                                            # survivor_id followed the re-key
