from datetime import datetime, timedelta, timezone

from kb_core.domain import NodeRecord
from kb_core.entities import (
    BELONGS_TO, ENTITY_CLASS, ENTITY_SOURCE, MENTIONS, UNTYPED_CATEGORY, instance_external_id,
    normalize_entity_name,
)
from kb_core.identity import node_id
from kb_core.ingest.promotion import EntityPromotionWorker


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


def _instance_id(name):
    """Canonical instance node id — identity is name only now (category is a link)."""
    return node_id(ENTITY_SOURCE, instance_external_id(name))


def _stage(kb, eid, category, name):
    kb.stage_entity_candidate(kb.upsert(_mem(eid)).id, category, name)


def _stage_class(kb, eid, name):
    """Mimic the discover pass: an inactive placeholder + one per-memory vote."""
    m = kb.upsert(_mem(eid))
    kb.upsert_entity_class(name, active=False)
    kb.stage_entity_candidate(m.id, ENTITY_CLASS, name)
    return m


def _stage_at(kb, eid, category, name, *, days_ago, tags=("email",)):
    """Stage a candidate on a memory whose event_at is `days_ago` days back."""
    rec = NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid,
                     event_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
                     tags=list(tags))
    kb.stage_entity_candidate(kb.upsert(rec).id, category, name)


def test_dormant_thread_mentions_do_not_promote(kb):
    """3 mentions all ~a year old: decayed support ~0.6 < 3 — no first-time creation.
    (Undated memories keep factor 1.0, so the existing threshold tests above pin the
    pre-decay behavior unchanged.)"""
    for i in range(3):
        _stage_at(kb, f"o{i}", "people", "Avery", days_ago=365)
    assert kb.promotable_entities(threshold=3) == []
    assert EntityPromotionWorker(kb, threshold=3).run() == 0


def test_mixed_age_mentions_promote_when_decayed_sum_clears(kb):
    """3 fresh (factor ~1.0) + 2 old mentions: sum ~3.4 >= 3 — promotes."""
    for i in range(3):
        _stage_at(kb, f"f{i}", "people", "Avery", days_ago=0)
    for i in range(2):
        _stage_at(kb, f"s{i}", "people", "Avery", days_ago=365)
    assert EntityPromotionWorker(kb, threshold=3).run() == 1
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Avery"]


def test_established_entity_links_old_mentions_immediately(kb):
    """The entity_exists leg is exempt from decay: once established, even a year-old
    mention links on the next sweep — no threshold, no age gate."""
    for i in range(3):
        _stage(kb, f"a{i}", "people", "Avery")
    EntityPromotionWorker(kb, threshold=3).run()
    _stage_at(kb, "a3", "people", "Avery", days_ago=365)
    groups = kb.promotable_entities(threshold=3)
    assert len(groups) == 1 and groups[0]["entity_exists"] is True
    assert EntityPromotionWorker(kb, threshold=3).run() == 1


def test_event_kind_promotes_from_one_old_memory(kb):
    """The event-kind leg stays on the raw count: age_factor < 1.0 for anything older
    than a day, so a decayed sum could never reach the >= 1 bar and would silently kill
    all event promotion (by design: exempt legs)."""
    kb.upsert_entity_class("meeting", kind="event")
    _stage_at(kb, "e0", "meeting", "Quarterly Review", days_ago=365)
    groups = kb.promotable_entities(threshold=3)
    assert [g["normalized_name"] for g in groups] == \
        [normalize_entity_name("Quarterly Review")]


def test_worker_promotes_at_threshold_and_consumes_candidates(kb):
    for i in range(3):
        _stage(kb, f"b{i}", "people", "Avery")
    assert EntityPromotionWorker(kb, threshold=3).run() == 1
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Avery"]
    assert kb.promotable_entities(threshold=3) == []        # candidates consumed


def test_worker_does_not_promote_below_threshold(kb):
    _stage(kb, "x0", "people", "Avery")
    _stage(kb, "x1", "people", "Avery")                    # only 2 < threshold 3
    assert EntityPromotionWorker(kb, threshold=3).run() == 0
    assert kb.instances_of("people") == []


def test_threshold_then_link_new_mention_immediately(kb):
    # establish the entity via the threshold
    for i in range(3):
        _stage(kb, f"a{i}", "people", "Avery")
    EntityPromotionWorker(kb, threshold=3).run()
    inst_id = _instance_id("Avery")
    assert len(kb.memories_linked(inst_id, link_type=MENTIONS)) == 3

    # a single NEW memory mentions Avery — below threshold, but the entity now exists,
    # so the promotion sweep links it immediately.
    _stage(kb, "a3", "people", "Avery")
    groups = kb.promotable_entities(threshold=3)
    assert len(groups) == 1 and groups[0]["entity_exists"] is True and groups[0]["memory_count"] == 1
    assert EntityPromotionWorker(kb, threshold=3).run() == 1
    assert len(kb.memories_linked(inst_id, link_type=MENTIONS)) == 4


def test_promotion_representative_cannot_flip_casing(kb):
    """A past bug: promotable_entities' representative is max(ec.raw_name) over the
    currently-unpromoted rows of a normalized_name group -- byte-order max, tick-dependent on
    whatever casing the latest re-analysis pass happened to (re-)stage (knowledge_base.py's
    re-analysis DELETEs+re-INSERTs unpromoted candidates per memory). Once the instance already
    exists, `entity_exists` bypasses the recurrence threshold entirely, so promote_entity fires
    immediately with that unstable representative on every fresh mention. Establish 'Avery',
    then stage a single opposite-casing re-mention ('avery') -- the sole unpromoted row in its
    group, so it IS the representative -- and confirm the sweep does not flip canonical_name."""
    mems, inst_id = _promote_avery(kb)                     # establishes canonical_name "Avery"
    _stage(kb, "e_new", "people", "avery")                 # re-analysis restages, opposite casing
    groups = kb.promotable_entities(threshold=3)
    assert len(groups) == 1 and groups[0]["entity_exists"] is True and groups[0]["raw_name"] == "avery"
    # run()'s return is an AGGREGATE (promotions + reconcile passes + class churn) —
    # asserting it pins plumbing this test does not care about and inherits cross-run
    # counter noise. The property under test is the name; the promoted flag pins that
    # the sweep really consumed the group.
    EntityPromotionWorker(kb, threshold=3).run()
    assert kb.promotable_entities(threshold=3) == []             # group consumed
    assert kb.get_entity(inst_id).canonical_name == "Avery"    # unchanged, not "avery"


def test_worker_activates_class_at_threshold_and_backfills_belongs_to(kb):
    mems = [_stage_class(kb, f"c{i}", "people") for i in range(3)]
    assert kb.entity_class_names() == []                    # inactive until the sweep
    assert EntityPromotionWorker(kb, threshold=3).run() == 1
    assert kb.entity_class_names() == ["people"]            # flipped active
    cls = kb.get_entity_class("people")
    voters = kb.memories_linked(cls.node.id, link_type=BELONGS_TO)
    assert {n.id for n in voters} == {m.id for m in mems}   # every voter backfilled
    assert kb.activatable_classes(threshold=3) == []        # candidates consumed


def test_worker_does_not_activate_class_below_threshold(kb):
    _stage_class(kb, "p0", "people")
    _stage_class(kb, "p1", "people")                        # only 2 < threshold 3
    assert EntityPromotionWorker(kb, threshold=3).run() == 0
    assert kb.entity_class_names() == []
    assert kb.get_entity_class("people").active is False


def test_reconcile_backfills_voters_on_activation_and_is_idempotent(kb):
    mems = [_stage_class(kb, f"a{i}", "people") for i in range(3)]
    res = kb.reconcile_classes(floor=3, cap=20, dominance_hi=0.6, dominance_lo=0.6)
    assert res["activated"] == ["people"]                   # floor + dominance (sole leader)
    cls = kb.get_entity_class("people")
    # activation backfills belongs_to from every staged voter
    assert {n.id for n in kb.memories_linked(cls.node.id, link_type=BELONGS_TO)} == \
           {m.id for m in mems}
    # re-running on the same votes is a no-op (already active, still dominant) — no flapping
    res2 = kb.reconcile_classes(floor=3, cap=20, dominance_hi=0.6, dominance_lo=0.6)
    assert res2["activated"] == [] and res2["deactivated"] == []


def test_class_candidates_are_not_promoted_as_instances(kb):
    for i in range(3):
        _stage_class(kb, f"g{i}", "people")
    # a class candidate must never be mistaken for an instance candidate
    assert kb.promotable_entities(threshold=1) == []
    EntityPromotionWorker(kb, threshold=3).run()
    assert kb.instances_of("people") == []                  # no bogus instance node created


def _promote_avery(kb, eids=("d0", "d1", "d2")):
    """Establish instance people/Avery from `eids` memories; return their nodes + instance id."""
    mems = [kb.upsert(_mem(e)) for e in eids]
    for m in mems:
        kb.stage_entity_candidate(m.id, "people", "Avery")
    EntityPromotionWorker(kb, threshold=3).run()
    return mems, _instance_id("Avery")


def _sever_links(pool, inst_id, *, keep=0):
    """Drop the instance's mention links directly, keeping `keep` of them. Goes straight to SQL
    rather than through delete()/retract (which now auto-collect), so reconcile_instances can be
    exercised in isolation against a known orphan state."""
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM entity_links WHERE entity_id = %(e)s AND memory_id IN ("
            "    SELECT memory_id FROM entity_links WHERE entity_id = %(e)s OFFSET %(k)s)",
            {"e": inst_id, "k": keep})


def test_reconcile_instances_collects_zero_support_instance(kb, pool):
    _, inst_id = _promote_avery(kb)
    _sever_links(pool, inst_id)                             # every supporter gone, instance stranded
    assert kb.reconcile_instances() == 1
    assert kb.get_entity(inst_id) is None
    assert kb.reconcile_instances() == 0                    # idempotent


def test_reconcile_instances_keeps_instance_with_a_surviving_mention(kb, pool):
    _, inst_id = _promote_avery(kb)
    _sever_links(pool, inst_id, keep=1)                     # one memory still mentions Avery
    assert kb.reconcile_instances() == 0                    # a live mention is not an orphan
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Avery"]
    assert len(kb.memories_linked(inst_id, link_type=MENTIONS)) == 1


def test_reconcile_instances_spares_instance_with_a_pending_candidate(kb, pool):
    _, inst_id = _promote_avery(kb)
    _sever_links(pool, inst_id)                             # zero materialized support...
    # ...but a fresh memory just staged a mention not yet linked: the instance is about to be
    # re-supported, so GC must not race ahead of the next promotion sweep.
    kb.stage_entity_candidate(kb.upsert(_mem("d_new")).id, "people", "Avery")
    assert kb.reconcile_instances() == 0
    assert kb.get_entity(inst_id) is not None
    EntityPromotionWorker(kb, threshold=3).run()            # the pending mention links through
    assert len(kb.memories_linked(inst_id, link_type=MENTIONS)) == 1


def test_reconcile_instances_never_collects_class_nodes(kb, pool):
    # promoting an instance mints its category CLASS node (entity_type='class') with no belongs_to
    # links of its own — GC must scope to instances and leave the class standing.
    _, inst_id = _promote_avery(kb)
    _sever_links(pool, inst_id)
    assert kb.reconcile_instances() == 1                    # the instance only
    assert kb.get_entity_class("people") is not None        # the category survives


def test_worker_run_collects_orphans_in_the_sweep(kb, pool):
    _, inst_id = _promote_avery(kb)
    _sever_links(pool, inst_id)                             # orphaned out-of-band
    assert EntityPromotionWorker(kb, threshold=3).run() == 1  # the scheduled sweep GCs it
    assert kb.instances_of("people") == []


def test_delete_collects_orphan_inline(kb):
    # the delete() path collects an instance the moment its last supporting note is removed —
    # no waiting for the sweep.
    mems, inst_id = _promote_avery(kb)
    kb.delete(mems[0].id)
    kb.delete(mems[1].id)
    assert kb.get_entity(inst_id) is not None              # one supporter remains
    kb.delete(mems[2].id)                                  # last supporter gone
    assert kb.get_entity(inst_id) is None                  # delete() collected it inline
    assert kb.instances_of("people") == []


def _mint_junk_instance(kb, category, name, eid):
    """A junk-named instance WITH one supporting mention, created before the gate existed —
    candidate inserted directly (the staging gate would reject the name), then promoted so the
    mention link backfills. Support=1 means the zero-support GC won't remove it; only the
    name-based prune should."""
    m = kb.upsert(_mem(eid))
    with kb._pool.connection() as conn:
        conn.execute(
            "INSERT INTO entity_candidates (memory_id, entity_type, normalized_name, raw_name) "
            "VALUES (%s, %s, %s, %s)", (m.id, category, normalize_entity_name(name), name))
    kb.promote_entity(category, name)


def test_noisy_instances_flags_and_prunes_junk(kb):
    _mint_junk_instance(kb, "notes", "README.md", "jm")
    _promote_avery(kb)                                     # a real instance (+ runs a sweep)
    assert [d["name"] for d in kb.noisy_instances()] == ["README.md"]  # survived GC, name-flagged
    assert kb.prune_noisy_instances() == 1
    assert kb.get_entity(_instance_id("README.md")) is None
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Avery"]


def test_junk_candidate_never_resurrects_a_pruned_instance(kb):
    # leftover pre-gate candidates (inserted directly, bypassing the staging gate) must not
    # re-promote a junk instance on a later sweep
    with kb._pool.connection() as conn:
        for i in range(3):
            m = kb.upsert(_mem(f"j{i}"))
            conn.execute(
                "INSERT INTO entity_candidates (memory_id, entity_type, normalized_name, raw_name) "
                "VALUES (%s, 'notes', 'readme.md', 'README.md')", (m.id,))
    assert kb.promotable_entities(threshold=1) == []       # junk filtered out of promotion
    assert EntityPromotionWorker(kb, threshold=1).run() == 0
    assert kb.instances_of("notes") == []


def test_class_floor_independent_of_instance_threshold(kb):
    # instance "Avery" recurs across 2 memories -> promotes at the instance threshold (2)
    _stage(kb, "i0", "people", "Avery")
    _stage(kb, "i1", "people", "Avery")
    # class "projects" voted by only 2 memories -> below the class vote floor (3)
    _stage_class(kb, "j0", "projects")
    _stage_class(kb, "j1", "projects")
    EntityPromotionWorker(kb, threshold=2, class_floor=3).run()
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Avery"]  # promoted at 2
    assert kb.get_entity_class("projects").active is False                      # class needs 3
    # the category 'people' exists to nest the instance but is NOT auto-activated (no votes)
    assert kb.get_entity_class("people").active is False


def test_untyped_instance_promotes_with_no_class_link(kb):
    for i in range(2):
        m = kb.upsert(_mem(f"u{i}"))
        kb.stage_entity_candidate(m.id, UNTYPED_CATEGORY, "Mystery Thing")
    assert EntityPromotionWorker(kb, threshold=2).run() == 1
    inst = kb.get_entity(_instance_id("Mystery Thing"))
    assert inst is not None and inst.entity_type == "instance"
    # mention link exists, but NO is_a class link
    assert kb.entity_class_link_pairs() == []
    assert len(kb.memories_linked(inst.node.id, link_type="mentions")) == 2


def test_active_class_names_returns_only_active(kb):
    kb.upsert_entity_class("organization", active=True)
    kb.upsert_entity_class("project", active=False)
    assert kb.active_class_names() == {"organization"}


def test_old_untagged_note_mentions_still_promote(kb):
    """Amendment scope gate: dated NOTES never decay — 3 year-old note mentions
    still sum to 3.0 and promote."""
    for i in range(3):
        _stage_at(kb, f"n{i}", "people", "Avery", days_ago=365, tags=())
    assert EntityPromotionWorker(kb, threshold=3).run() == 1
