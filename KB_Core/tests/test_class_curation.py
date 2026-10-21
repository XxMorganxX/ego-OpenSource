"""Safeguards + curation for entity classes: the active-class cap, the blocked tombstone,
and the human-driven merge / reparent operations (migration 0011)."""

import string

from kb_core.domain import NodeRecord
from kb_core.entities import BELONGS_TO, ENTITY_CLASS, MENTIONS


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note",
                      content="c", slug=eid)


def _class_with_members(kb, name, n):
    """An active class that `n` distinct memories belong_to (its rank signal)."""
    cls = kb.upsert_entity_class(name, active=True)
    for i in range(n):
        mem = kb.upsert(_mem(f"{name}-m{i}"))
        kb.link_entity(mem.id, cls.node.id, BELONGS_TO)
    return cls


# ---- Layer 2: the top-N competitive cap --------------------------------------

def test_offered_classes_ranked_by_member_count(kb):
    _class_with_members(kb, "alpha", 3)
    _class_with_members(kb, "beta", 1)
    _class_with_members(kb, "gamma", 0)
    # default cap shows them all, strongest first; ties broken by name
    assert kb.entity_class_names() == ["alpha", "beta", "gamma"]


def test_cap_demotes_the_weakest_class(kb):
    _class_with_members(kb, "alpha", 3)
    _class_with_members(kb, "beta", 2)
    _class_with_members(kb, "gamma", 1)
    # a recurring-but-rarely-used class falls out of the offered set without a status change
    assert kb.entity_class_names(limit=2) == ["alpha", "beta"]
    # gamma is still active — just not offered
    assert kb.get_entity_class("gamma").active is True


def test_inactive_classes_are_not_offered(kb):
    kb.upsert_entity_class("provisional", active=False)
    assert kb.entity_class_names() == []


# ---- Layer 3: the blocked tombstone ------------------------------------------

def test_class_voters_lists_memories_that_fed_the_class(kb):
    m1, m2 = kb.upsert(_mem("m1")), kb.upsert(_mem("m2"))
    kb.stage_entity_candidate(m1.id, ENTITY_CLASS, "people")    # a discover-pass vote
    cls = kb.upsert_entity_class("people", active=True)
    kb.link_entity(m2.id, cls.node.id, BELONGS_TO)              # a classified member
    voters = {n.id for n in kb.class_voters("people")}
    assert voters == {m1.id, m2.id}                            # union of votes + members


def test_block_requeues_voter_memories_for_reenrichment(kb):
    mem = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(mem.id, ENTITY_CLASS, "junk")
    kb.upsert_entity_class("junk", active=True)
    kb.mark_enriched(mem.id)                                    # pretend it's been enriched
    assert kb.get(mem.id).enrichment_status == "done"
    kb.block_class("junk")                                      # requeue_voters defaults True
    assert kb.get(mem.id).enrichment_status == "pending"        # its voter is re-queued


def test_block_without_requeue_leaves_enrichment_alone(kb):
    mem = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(mem.id, ENTITY_CLASS, "junk")
    kb.upsert_entity_class("junk", active=True)
    kb.mark_enriched(mem.id)
    kb.block_class("junk", requeue_voters=False)
    assert kb.get(mem.id).enrichment_status == "done"          # untouched


def test_merge_does_not_requeue_source_voters(kb):
    mem = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(mem.id, ENTITY_CLASS, "ppl")
    kb.upsert_entity_class("ppl", active=True)
    kb.mark_enriched(mem.id)
    kb.merge_classes("ppl", "people")
    assert kb.get(mem.id).enrichment_status == "done"          # merge re-points, no re-enrich


def test_block_hides_class_and_purges_instances(kb):
    kb.promote_entity("people", "Avery")              # an instance under 'people'
    kb.upsert_entity_class("people", active=True)      # category is active (e.g. via votes)
    assert kb.get_entity_class("people").active is True
    blocked = kb.block_class("people")
    assert blocked.status == "blocked"
    assert kb.entity_class_names() == []               # withheld from the offered list
    assert kb.instances_of("people") == []             # instance purged
    # default class listing hides it; include_blocked reveals it
    assert kb.list_entity_classes() == []
    assert [c["name"] for c in kb.list_entity_classes(include_blocked=True)] == ["people"]


def test_blocked_class_cannot_be_revoted(kb, pool):
    """The core safeguard: a tombstoned category can't re-accrete votes, so a consistently
    re-emitted hallucination stays gone instead of re-activating on the next sweep."""
    kb.upsert_entity_class("junk", active=True)
    kb.block_class("junk")
    mem = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(mem.id, ENTITY_CLASS, "junk")     # dropped silently
    with pool.connection() as conn:
        n = conn.execute("SELECT count(*) AS c FROM entity_candidates").fetchone()["c"]
    assert n == 0
    # and even if a stale candidate existed, activation skips blocked classes
    assert [g["raw_name"] for g in kb.activatable_classes(threshold=1)] == []


def test_blocked_instance_candidate_is_dropped(kb, pool):
    kb.upsert_entity_class("people", active=True)
    kb.block_class("people")
    mem = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(mem.id, "people", "Avery")        # instance under blocked cat
    with pool.connection() as conn:
        n = conn.execute("SELECT count(*) AS c FROM entity_candidates").fetchone()["c"]
    assert n == 0


def test_upsert_never_revives_a_blocked_class(kb):
    kb.upsert_entity_class("junk", active=True)
    kb.block_class("junk")
    kb.upsert_entity_class("junk", active=True)                  # discover/promote tries again
    assert kb.get_entity_class("junk").status == "blocked"


def test_unblock_returns_class_as_inactive(kb):
    kb.upsert_entity_class("people", active=True)
    kb.block_class("people")
    ent = kb.unblock_class("people")
    assert ent.status == "inactive"                              # must re-earn activation
    assert kb.entity_class_names() == []


# ---- Layer 3: merge & reparent -----------------------------------------------

def test_merge_folds_members_and_tombstones_source(kb):
    src = _class_with_members(kb, "ppl", 2)
    _class_with_members(kb, "people", 1)
    merged = kb.merge_classes("ppl", "people")
    assert merged.canonical_name == "people"
    assert "ppl" in merged.aliases                              # old name preserved as alias
    assert kb.get_entity_class("ppl").status == "blocked"       # source gone for good
    # the 2 memories from ppl now belong_to people (plus its own 1) -> 3 distinct
    assert kb.memories_linked(merged.node.id, link_type=BELONGS_TO).__len__() == 3
    assert kb.entity_class_names() == ["people"]


def test_reparent_recategorizes_instance_keeping_node_and_mentions(kb):
    inst = kb.promote_entity("animals", "Fido")                  # mis-filed under 'animals'
    mem = kb.upsert(_mem("m1"))
    kb.link_entity(mem.id, inst.node.id, MENTIONS, weight=0.7)
    moved = kb.reparent_instance(inst.node.id, "pets")
    assert moved.node.id == inst.node.id                        # same canonical node, no re-key
    assert moved.entity_type == "instance"
    assert [c.canonical_name for c in kb.classes_of(moved.node.id)] == ["pets"]  # recategorized
    assert kb.instances_of("animals") == []                    # no longer a member of 'animals'
    assert {n.id for n in kb.memories_linked(moved.node.id, link_type=MENTIONS)} == {mem.id}


# ---- full wipe + re-enrichment primitives ------------------------------------

def test_reset_entities_wipes_graph_but_keeps_memories(kb, pool):
    mem = kb.upsert(_mem("m1"))
    cls = kb.upsert_entity_class("people", active=True)
    kb.link_entity(mem.id, cls.node.id, BELONGS_TO)
    kb.promote_entity("people", "Avery")                       # an instance + its links
    kb.stage_entity_candidate(mem.id, ENTITY_CLASS, "projects")  # a pending vote
    counts = kb.reset_entities()
    assert counts["entities"] >= 2                              # class + instance removed
    assert kb.list_entities() == []                            # whole entity graph gone
    assert kb.entity_class_names() == []
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS c FROM entity_links").fetchone()["c"] == 0
        assert conn.execute("SELECT count(*) AS c FROM entity_candidates").fetchone()["c"] == 0
    assert kb.get(mem.id) is not None                          # the memory is untouched


def test_requeue_enrichment_forces_pending_regardless_of_hash(kb, pool):
    mem = kb.upsert(_mem("m1"))
    with pool.connection() as conn:                            # simulate an already-enriched node
        conn.execute("UPDATE nodes SET enrichment_status = 'done', "
                     "enriched_hash = content_hash WHERE id = %s", (mem.id,))
    n = kb.requeue_enrichment(node_type="note")
    assert n == 1
    assert kb.get(mem.id).enrichment_status == "pending"       # re-queued despite matching hash


# ---- vote-driven activation (dominance + floor + cap + hysteresis) -----------

def _votes(kb, name, n, *, start=0):
    """Stage `n` distinct memory votes for class `name` (the discover-pass signal), ensuring
    the class node exists (inactive). Returns the memory ids that voted."""
    kb.upsert_entity_class(name, active=False)
    ids = []
    for i in range(n):
        mem = kb.upsert(_mem(f"{name}-v{start + i}"))
        kb.stage_entity_candidate(mem.id, ENTITY_CLASS, name)
        ids.append(mem.id)
    return ids


def test_dominance_threshold_interpolates_and_clamps():
    from kb_core.knowledge_base import dominance_threshold
    assert dominance_threshold(0, hi=0.6, lo=0.4, span=40) == 0.6      # few classes -> strict
    assert dominance_threshold(20, hi=0.6, lo=0.4, span=40) == 0.5     # halfway
    assert dominance_threshold(40, hi=0.6, lo=0.4, span=40) == 0.4     # at span -> lenient
    assert dominance_threshold(999, hi=0.6, lo=0.4, span=40) == 0.4    # clamped at lo


def test_deactivate_class_flips_to_inactive_and_keeps_votes(kb, pool):
    _votes(kb, "people", 1)
    kb.upsert_entity_class("people", active=True)
    assert kb.deactivate_class("people").status == "inactive"
    with pool.connection() as conn:                            # the vote ledger survives
        c = conn.execute("SELECT count(*) AS c FROM entity_candidates "
                         "WHERE entity_type='class' AND normalized_name='people'").fetchone()["c"]
    assert c == 1
    # a blocked tombstone is never demoted to inactive by deactivate
    kb.upsert_entity_class("junk", active=True)
    kb.block_class("junk", requeue_voters=False, purge_instances=False)
    assert kb.deactivate_class("junk").status == "blocked"


def test_reconcile_activates_all_above_floor_when_under_cap(kb):
    # A skewed leader must NOT lock out smaller legitimate categories while there's cap room:
    # dominance only competes once more than `cap` classes clear the floor.
    _votes(kb, "tasks", 57); _votes(kb, "projects", 13); _votes(kb, "people", 12)
    _votes(kb, "dates", 2)                                     # below floor -> excluded
    res = kb.reconcile_classes(floor=3, cap=20, dominance_hi=0.6, dominance_lo=0.4, margin=0.1)
    assert set(res["activated"]) == {"tasks", "projects", "people"}
    assert "dates" not in res["activated"]


def test_reconcile_caps_active_classes_to_top_n_by_votes(kb):
    for i in range(5):
        _votes(kb, f"c{i}", 5 + i)                             # c0=5 .. c4=9, all >= floor
    res = kb.reconcile_classes(floor=3, cap=3, dominance_hi=0.4, dominance_lo=0.4, margin=0.1)
    assert set(res["activated"]) == {"c4", "c3", "c2"}         # top 3 by votes only


def test_reconcile_dominance_prunes_weak_class_within_cap_when_competing(kb):
    # 4 classes > cap=3 -> competing -> even a top-cap class below p*leader is pruned
    _votes(kb, "tasks", 20); _votes(kb, "alpha", 18)
    _votes(kb, "beta", 4); _votes(kb, "gamma", 3)
    res = kb.reconcile_classes(floor=3, cap=3, dominance_hi=0.6, dominance_lo=0.6, margin=0.1)
    # top-3 by votes = tasks(20), alpha(18), beta(4); dominance (>=0.6*20=12) drops beta
    assert set(res["activated"]) == {"tasks", "alpha"}


def test_reconcile_keeps_active_class_inside_hysteresis_band(kb):
    # competing (4 > cap 3) so dominance applies; then the leader grows, dropping a class's
    # ratio into the [p-margin, p) band where it should HOLD, not flap.
    for c in ("tasks", "alpha", "beta"):
        _votes(kb, c, 12)
    _votes(kb, "gamma", 4)                                     # 4th, kept out by the cap
    kb.reconcile_classes(floor=3, cap=3, dominance_hi=0.6, dominance_lo=0.6, margin=0.1)
    _votes(kb, "tasks", 10, start=100)                         # leader tasks -> 22
    # beta ratio = 12/22 = 0.545: below activation (0.6 -> 13.2) but above deact (0.5 -> 11) -> hold
    res = kb.reconcile_classes(floor=3, cap=3, dominance_hi=0.6, dominance_lo=0.6, margin=0.1)
    assert "beta" not in res["deactivated"]
    assert kb.get_entity_class("beta").status == "active"


def test_reconcile_deactivates_class_below_hysteresis_floor(kb):
    for c in ("tasks", "alpha", "beta"):
        _votes(kb, c, 12)
    _votes(kb, "gamma", 4)
    kb.reconcile_classes(floor=3, cap=3, dominance_hi=0.6, dominance_lo=0.6, margin=0.1)
    assert kb.get_entity_class("beta").status == "active"
    _votes(kb, "tasks", 18, start=100)                         # leader tasks -> 30
    # beta ratio = 12/30 = 0.4: below the deactivation bar (0.5 -> 15) -> demote
    res = kb.reconcile_classes(floor=3, cap=3, dominance_hi=0.6, dominance_lo=0.6, margin=0.1)
    assert "beta" in res["deactivated"]
    assert kb.get_entity_class("beta").status == "inactive"


def test_reconcile_never_activates_blocked(kb):
    _votes(kb, "junk", 10)
    kb.block_class("junk", requeue_voters=False, purge_instances=False)
    res = kb.reconcile_classes(floor=3, cap=20, dominance_hi=0.4, dominance_lo=0.4, margin=0.1)
    assert "junk" not in res["activated"]
    assert kb.get_entity_class("junk").status == "blocked"


def test_votes_record_memory_and_survive_activation_cycle(kb):
    voters = set(_votes(kb, "people", 3))
    assert {n.id for n in kb.class_voters("people")} == voters         # who voted is queryable
    kb.reconcile_classes(floor=3, cap=20, dominance_hi=0.4, dominance_lo=0.4, margin=0.1)
    assert kb.get_entity_class("people").status == "active"
    kb.deactivate_class("people")
    assert {n.id for n in kb.class_voters("people")} == voters         # ...and survives the cycle


def test_promotion_worker_reconciles_classes(kb):
    from kb_core.ingest.promotion import EntityPromotionWorker
    _votes(kb, "people", 5); _votes(kb, "projects", 4)
    EntityPromotionWorker(kb, class_floor=3, max_classes=20,
                          dominance_hi=0.4, dominance_lo=0.4).run()
    assert kb.get_entity_class("people").status == "active"
    assert kb.get_entity_class("projects").status == "active"


def test_set_class_description_locks_against_slm_overwrite(kb):
    kb.upsert_entity_class("widgets", active=True, description="auto draft")
    kb.set_class_description("widgets", "human-curated definition")
    assert kb.get_entity_class("widgets").description == "human-curated definition"
    # the discover pass re-drafts; the human lock holds
    kb.upsert_entity_class("widgets", active=False, description="slm tries again")
    assert kb.get_entity_class("widgets").description == "human-curated definition"


def test_unlocked_description_is_refinable_but_never_wiped(kb):
    kb.upsert_entity_class("gadgets", active=True, description="first draft")
    # the SLM may refine its own (unlocked) draft
    kb.upsert_entity_class("gadgets", active=False, description="better draft")
    assert kb.get_entity_class("gadgets").description == "better draft"
    # a call with no description (e.g. instance promotion ensuring the class) must not wipe it
    kb.upsert_entity_class("gadgets", active=False)
    assert kb.get_entity_class("gadgets").description == "better draft"


def test_set_class_description_missing_class_returns_none(kb):
    assert kb.set_class_description("ghosts", "x") is None


def test_set_class_kind_overwrites_seeded_and_unseeded_kind(kb):
    kb.upsert_entity_class("gizmo", active=True)          # unrecognized name -> kind starts NULL
    assert kb.get_entity_class("gizmo").node  # sanity: class exists
    kb.set_class_kind("gizmo", "system")
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT kind FROM entities WHERE canonical_name = 'gizmo'").fetchone()
    assert row["kind"] == "system"
    # a later curated write overwrites even a previously-set kind (it IS the curated path)
    kb.set_class_kind("gizmo", "artifact")
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT kind FROM entities WHERE canonical_name = 'gizmo'").fetchone()
    assert row["kind"] == "artifact"


def test_set_class_kind_rejects_unknown_kind(kb):
    kb.upsert_entity_class("gizmo", active=True)
    try:
        kb.set_class_kind("gizmo", "not-a-real-kind")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_class_kind_missing_class_returns_none(kb):
    assert kb.set_class_kind("ghosts", "system") is None


def test_entity_classes_with_descriptions_pairs_name_and_text(kb):
    kb.upsert_entity_class("alpha", active=True, description="the alpha things")
    kb.upsert_entity_class("beta", active=True)  # no description
    pairs = dict(kb.entity_classes_with_descriptions())
    assert pairs["alpha"] == "the alpha things"
    assert pairs["beta"] == ""   # empty string, never None


def test_classes_for_prompt_surfaces_active_then_top_candidates(kb):
    # active class with description -> shown first, WITH its description
    active = _class_with_members(kb, "projects", 2)
    kb.set_class_description("projects", "things we build")
    assert active.active is True
    # an inactive candidate with votes -> shown after, name-only (description withheld)
    kb.upsert_entity_class("databases", active=False, description="should stay hidden")
    for i in range(3):
        m = kb.upsert(_mem(f"d{i}"))
        kb.stage_entity_candidate(m.id, ENTITY_CLASS, "databases")

    pairs = kb.entity_classes_for_prompt()
    assert [n for n, _ in pairs] == ["projects", "databases"]  # active first, then candidate
    by_name = dict(pairs)
    assert by_name["projects"] == "things we build"  # active carries its grounding
    assert by_name["databases"] == ""                # inactive: name only, no description


def test_classes_for_prompt_excludes_blocked_and_respects_limit(kb):
    _class_with_members(kb, "alpha", 3)
    _class_with_members(kb, "beta", 2)
    kb.upsert_entity_class("tombstoned", active=False)
    kb.block_class("tombstoned")  # human-removed -> never offered
    offered = [n for n, _ in kb.entity_classes_for_prompt(limit=1)]
    assert offered == ["alpha"]                       # cap honored, strongest kept
    allnames = [n for n, _ in kb.entity_classes_for_prompt()]
    assert "tombstoned" not in allnames               # blocked tombstone excluded


def test_set_class_pinned_toggles_and_is_reflected_on_entity(kb):
    kb.upsert_entity_class("people", active=True)
    assert kb.get_entity_class("people").pinned is False
    pinned = kb.set_class_pinned("people", True)
    assert pinned.pinned is True
    assert kb.get_entity_class("people").pinned is True
    kb.set_class_pinned("people", False)
    assert kb.get_entity_class("people").pinned is False


def test_set_class_pinned_returns_none_for_missing_class(kb):
    assert kb.set_class_pinned("ghost", True) is None


def test_reconcile_does_not_deactivate_a_pinned_class(kb):
    # A class with zero votes is normally deactivated by reconcile; pinning protects it.
    kb.upsert_entity_class("manual-cat", active=True)
    kb.set_class_pinned("manual-cat", True)
    result = kb.reconcile_classes(floor=1)
    assert "manual-cat" not in result["deactivated"]
    assert kb.get_entity_class("manual-cat").status == "active"


def test_reconcile_still_deactivates_an_unpinned_zero_vote_class(kb):
    kb.upsert_entity_class("drifter", active=True)
    result = kb.reconcile_classes(floor=1)
    assert "drifter" in result["deactivated"]


def test_list_entity_classes_includes_pinned(kb):
    kb.upsert_entity_class("people", active=True)
    kb.set_class_pinned("people", True)
    row = next(r for r in kb.list_entity_classes() if r["name"] == "people")
    assert row["pinned"] is True


def test_merge_requeues_source_voters_when_requested(kb):
    src = kb.upsert_entity_class("frameworks", active=True)
    tgt = kb.upsert_entity_class("tools", active=True)
    mem = kb.upsert(_mem("fw-note"))
    kb.link_entity(mem.id, src.node.id, BELONGS_TO)  # a member/voter of the source
    # mark it enriched so we can prove the merge resets it to pending
    kb.mark_enriched(mem.id)
    assert kb.get(mem.id).enrichment_status == "done"

    kb.merge_classes("frameworks", "tools", requeue_voters=True)
    assert kb.get(mem.id).enrichment_status == "pending"
    assert kb.get(mem.id).enriched_hash is None  # hash reset so re-enrichment isn't skipped
    assert kb.get_entity_class("frameworks").status == "blocked"  # source tombstoned


def test_merge_default_does_not_requeue(kb):
    src = kb.upsert_entity_class("frameworks", active=True)
    kb.upsert_entity_class("tools", active=True)
    mem = kb.upsert(_mem("fw-note"))
    kb.link_entity(mem.id, src.node.id, BELONGS_TO)
    kb.mark_enriched(mem.id)
    kb.merge_classes("frameworks", "tools")  # requeue_voters defaults to False
    assert kb.get(mem.id).enrichment_status == "done"


# ---- the discover pass never persists a degenerate SLM-drafted description ----

def test_upsert_drops_degenerate_description(kb):
    # the real failure mode: the small model collapsed into a repetition spiral while
    # drafting the description. It must not reach the column.
    garbage = "individuals or entities involved in a " + "r_e_" * 300
    kb.upsert_entity_class("people", active=True, description=garbage)
    assert kb.get_entity_class("people").description is None


def test_upsert_keeps_clean_description(kb):
    desc = "correspondence and messages received or sent"
    kb.upsert_entity_class("emails", active=True, description=desc)
    assert kb.get_entity_class("emails").description == desc


def test_default_cap_activates_a_long_tail_of_concepts(kb):
    from kb_core.knowledge_base import DEFAULT_MAX_ACTIVE_CLASSES
    assert DEFAULT_MAX_ACTIVE_CLASSES >= 60
    # 25 distinct concepts, each backed by 3 memories (clears floor=3). 25 <= cap,
    # so none compete -> the dominance ratio never engages and ALL 25 activate.
    mems = [kb.upsert(_mem(f"m{i}")) for i in range(3)]
    names = [f"concept {c}" for c in string.ascii_lowercase[:25]]   # "concept a" .. "concept y"
    for name in names:
        kb.upsert_entity_class(name, active=False)
        for m in mems:
            kb.stage_entity_candidate(m.id, ENTITY_CLASS, name)
    res = kb.reconcile_classes(floor=3)                       # cap defaults to the constant
    assert res["competing"] is False
    active = {e.canonical_name for e in kb.list_entities(entity_type="class") if e.active}
    assert active == set(names)
