"""The reconcile sweep's three predicates (is_a sweep, mentions sweep, reconcile_instances' GC
guard) matched entity_candidates
against ONLY canonical_key(instance.canonical_name) — never the instance's aliases[]. Consequence:
promote_entity's alias fold (`alias_owner`) links a folded-name mention onto the survivor, and the
SAME sweep's reconcile_instance_links immediately deletes it right back off (no candidate keys to
the survivor's canonical name). Same root cause silently strips merge_instances' re-pointed links.

These tests are written FIRST (TDD) against the buggy behavior, then the three SQL predicates in
`knowledge_base.py` are made alias-aware (candidate support matches canonical_key(canonical_name)
OR canonical_key of any alias) to make them pass.
"""
from kb_core.domain import NodeRecord
from kb_core.entities import ENTITY_SOURCE, MENTIONS, instance_external_id
from kb_core.identity import node_id
from kb_core.ingest.promotion import EntityPromotionWorker


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


def _instance_id(name):
    return node_id(ENTITY_SOURCE, instance_external_id(name))


def _stage(kb, eid, category, name):
    kb.stage_entity_candidate(kb.upsert(_mem(eid)).id, category, name)


# ---------------------------------------------------------------------------
# 1a. is_a sweep must not drop a class link only supported via an alias match
# ---------------------------------------------------------------------------

def test_is_a_sweep_keeps_link_supported_only_via_alias(kb):
    kb.upsert_entity_class("people", active=True)
    survivor = kb.upsert_entity("Avery Quinn", categories=["people"], aliases=["Avery"])
    # Only candidate support for this instance/category comes in under the OLD (aliased) name.
    _stage(kb, "a0", "people", "Avery")

    retracted = kb.reconcile_instance_links()

    assert retracted == 0
    assert [c.canonical_name for c in kb.classes_of(survivor.node.id)] == ["people"]


def test_is_a_sweep_still_drops_link_with_zero_support(kb):
    # Sanity: the alias-aware predicate must still retract when NOTHING (canonical or alias)
    # supports it — otherwise the fix would just always keep is_a links.
    kb.upsert_entity_class("people", active=True)
    survivor = kb.upsert_entity("Avery Quinn", categories=["people"], aliases=["Avery"])
    retracted = kb.reconcile_instance_links()
    assert retracted == 1
    assert kb.classes_of(survivor.node.id) == []


# ---------------------------------------------------------------------------
# 1b. mentions sweep must not drop a memory->instance link only supported via an alias match
# ---------------------------------------------------------------------------

def test_mentions_sweep_keeps_link_supported_only_via_alias(kb):
    survivor = kb.upsert_entity("Avery Quinn", aliases=["Avery"])
    mem = kb.upsert(_mem("b0"))
    kb.link_entity(mem.id, survivor.node.id, MENTIONS)
    # The only candidate that could back this mention is staged under the OLD (aliased) name —
    # this is exactly what merge_instances' re-pointed links + promote_entity's alias fold leave
    # behind: a mention with no candidate keyed to the survivor's own canonical name.
    _stage(kb, "b0", "people", "Avery")

    retracted = kb.reconcile_instance_links()

    assert retracted == 0
    mentioners = {n.id for n in kb.memories_linked(survivor.node.id, link_type=MENTIONS)}
    assert mem.id in mentioners


def test_mentions_sweep_still_drops_unsupported_mention(kb):
    survivor = kb.upsert_entity("Avery Quinn", aliases=["Avery"])
    mem = kb.upsert(_mem("b1"))
    kb.link_entity(mem.id, survivor.node.id, MENTIONS)
    # no candidate at all backs this mention (canonical or alias)
    retracted = kb.reconcile_instance_links()
    assert retracted == 1
    assert kb.memories_linked(survivor.node.id, link_type=MENTIONS) == []


# ---------------------------------------------------------------------------
# 1c. reconcile_instances' GC guard must not collect an instance with a PENDING alias-keyed
# candidate — the same "don't race ahead of the next promotion sweep" invariant as the
# canonical-name case, just extended to aliases.
# ---------------------------------------------------------------------------

def test_gc_guard_spares_instance_with_pending_alias_candidate(kb, pool):
    survivor = kb.upsert_entity("Avery Quinn", aliases=["Avery"])
    # zero materialized support (no entity_links at all)...
    assert kb.memories_linked(survivor.node.id, link_type=MENTIONS) == []
    # ...but a fresh mention of the OLD (aliased) name is staged, not yet linked/promoted.
    _stage(kb, "c0", "people", "Avery")

    assert kb.reconcile_instances() == 0
    assert kb.get_entity(survivor.node.id) is not None


def test_gc_guard_still_collects_truly_orphaned_instance(kb, pool):
    survivor = kb.upsert_entity("Avery Quinn", aliases=["Avery"])
    assert kb.memories_linked(survivor.node.id, link_type=MENTIONS) == []
    # no pending candidate anywhere (canonical or alias)
    assert kb.reconcile_instances() == 1
    assert kb.get_entity(survivor.node.id) is None


# ---------------------------------------------------------------------------
# 2. Worker-level integration test — the coverage hole that hid the alias bug. Mirrors the real flow:
# both instances are built via the actual promotion pipeline (stage + worker), then merged,
# then fresh mentions of the OLD name are staged and folded by a single worker.run() — which
# must survive its OWN reconcile sweep, and a subsequent run must not regress it either.
# ---------------------------------------------------------------------------

def test_worker_run_survives_alias_fold_end_to_end(kb):
    # Establish "Avery" as its own instance via the real promotion pipeline.
    m1 = kb.upsert(_mem("g1"))
    m2 = kb.upsert(_mem("g2"))
    kb.stage_entity_candidate(m1.id, "people", "Avery")
    kb.stage_entity_candidate(m2.id, "people", "Avery")
    EntityPromotionWorker(kb, threshold=2).run()
    avery_id = _instance_id("Avery")
    assert kb.get_entity(avery_id) is not None

    # Establish "Avery Quinn" as a separate instance, same category.
    ms1 = kb.upsert(_mem("g3"))
    ms2 = kb.upsert(_mem("g4"))
    kb.stage_entity_candidate(ms1.id, "people", "Avery Quinn")
    kb.stage_entity_candidate(ms2.id, "people", "Avery Quinn")
    EntityPromotionWorker(kb, threshold=2).run()
    averyquinn_id = _instance_id("Avery Quinn")
    assert kb.get_entity(averyquinn_id) is not None

    # Merge Avery into Avery Quinn: re-points m1/m2's mentions and records "Avery" as an
    # alias of the survivor. This is the pre-existing main-branch bug's setup too (merge_instances
    # re-pointed links, degraded by the live daemon's next reconcile sweep).
    kb.merge_instances(avery_id, averyquinn_id)
    assert kb.get_entity(avery_id) is None
    survivor = kb.get_entity(averyquinn_id)
    assert "Avery" in survivor.aliases

    # Fresh mentions of the OLD name land on NEW memories after the merge.
    n1 = kb.upsert(_mem("g5"))
    n2 = kb.upsert(_mem("g6"))
    kb.stage_entity_candidate(n1.id, "people", "Avery")
    kb.stage_entity_candidate(n2.id, "people", "Avery")

    # ONE worker run: promote_entity's alias fold links n1/n2 onto the survivor, then the SAME
    # tick's reconcile_instance_links/reconcile_instances sweep. Before the fix, that sweep drops
    # m1/m2 (re-pointed by the merge) AND n1/n2 (just folded) because neither is keyed to
    # canonical_key("Avery Quinn") — and no new "Avery" fragment must be re-minted either.
    EntityPromotionWorker(kb, threshold=2).run()

    mentioners = {n.id for n in kb.memories_linked(averyquinn_id, link_type=MENTIONS)}
    assert {m1.id, m2.id, ms1.id, ms2.id, n1.id, n2.id} <= mentioners
    assert kb.get_entity(avery_id) is None                 # no fragment re-minted

    # A SECOND run (nothing new staged) must not regress the re-pointed/folded links either —
    # the retraction sweep is idempotent across ticks, not just within the promoting one.
    EntityPromotionWorker(kb, threshold=2).run()
    mentioners2 = {n.id for n in kb.memories_linked(averyquinn_id, link_type=MENTIONS)}
    assert {m1.id, m2.id, ms1.id, ms2.id, n1.id, n2.id} <= mentioners2


# ---------------------------------------------------------------------------
# 4. promotable_entities' entity_exists arm must also fire when the name is owned as an
# ACTIVE instance's alias (not just its canonical name) — otherwise a single post-merge mention
# waits out the full recurrence threshold instead of linking immediately, and once it clears
# threshold it would promote as a brand-new instance rather than folding onto the survivor.
# ---------------------------------------------------------------------------

def test_entity_exists_true_via_alias_after_merge(kb):
    m1 = kb.upsert(_mem("h1"))
    m2 = kb.upsert(_mem("h2"))
    kb.stage_entity_candidate(m1.id, "people", "Avery")
    kb.stage_entity_candidate(m2.id, "people", "Avery")
    EntityPromotionWorker(kb, threshold=2).run()
    avery_id = _instance_id("Avery")

    ms1 = kb.upsert(_mem("h3"))
    ms2 = kb.upsert(_mem("h4"))
    kb.stage_entity_candidate(ms1.id, "people", "Avery Quinn")
    kb.stage_entity_candidate(ms2.id, "people", "Avery Quinn")
    EntityPromotionWorker(kb, threshold=2).run()
    averyquinn_id = _instance_id("Avery Quinn")

    kb.merge_instances(avery_id, averyquinn_id)

    # A SINGLE new "Avery" candidate — well below any reasonable threshold — must already show
    # entity_exists=True because the survivor owns "Avery" as an alias.
    n1 = kb.upsert(_mem("h5"))
    kb.stage_entity_candidate(n1.id, "people", "Avery")
    groups = kb.promotable_entities(threshold=5)
    matches = [g for g in groups if g["normalized_name"] == "avery"]
    assert len(matches) == 1
    assert matches[0]["entity_exists"] is True
    assert matches[0]["memory_count"] == 1

    kb.promote_entity("people", "Avery")
    mentioners = {n.id for n in kb.memories_linked(averyquinn_id, link_type=MENTIONS)}
    assert n1.id in mentioners
    assert kb.get_entity(avery_id) is None                 # still no fragment re-minted


# ---------------------------------------------------------------------------
# 5. alias_owner's LIMIT 1 must be deterministic when (pathologically) more than one active
# instance's aliases could match the same key.
# ---------------------------------------------------------------------------

def test_alias_owner_is_deterministic_with_multiple_matches(kb):
    a = kb.upsert_entity("Avery Quinn A", aliases=["Avery"])
    b = kb.upsert_entity("Avery Quinn B", aliases=["Avery"])
    expected = min(a.node.id, b.node.id)
    assert kb.alias_owner("Avery") == expected
    # stable across repeated calls
    assert kb.alias_owner("Avery") == expected


# ---------------------------------------------------------------------------
# 3. backfill_alias_links() — the one-time repair for links already stripped by the alias bug
# before this fix landed. Promoted candidates are a durable ledger, so lost links are
# reconstructible from them.
# ---------------------------------------------------------------------------

def test_backfill_alias_links_restores_missing_mention(kb):
    survivor = kb.upsert_entity("Avery Quinn", aliases=["Avery"])
    mem = kb.upsert(_mem("k0"))
    # A promoted candidate exists (the durable ledger)...
    kb.stage_entity_candidate(mem.id, "people", "Avery")
    with kb._pool.connection() as conn:
        conn.execute(
            "UPDATE entity_candidates SET promoted = true WHERE memory_id = %s", (mem.id,))
    # ...but the entity_links mention row is MISSING (exactly what the pre-fix reconcile sweep
    # would have stripped).
    assert kb.memories_linked(survivor.node.id, link_type=MENTIONS) == []

    inserted = kb.backfill_alias_links()

    assert inserted == 1
    mentioners = {n.id for n in kb.memories_linked(survivor.node.id, link_type=MENTIONS)}
    assert mem.id in mentioners


def test_backfill_alias_links_is_idempotent(kb):
    survivor = kb.upsert_entity("Avery Quinn", aliases=["Avery"])
    mem = kb.upsert(_mem("k1"))
    kb.stage_entity_candidate(mem.id, "people", "Avery")
    with kb._pool.connection() as conn:
        conn.execute(
            "UPDATE entity_candidates SET promoted = true WHERE memory_id = %s", (mem.id,))
    assert kb.backfill_alias_links() == 1
    assert kb.backfill_alias_links() == 0          # nothing left to restore


def test_backfill_alias_links_ignores_canonical_name_matches(kb):
    # A candidate keyed to the instance's own canonical name is not an "alias" repair case —
    # it's the normal path, and (by design) must be excluded from this backfill's count.
    survivor = kb.upsert_entity("Avery Quinn")
    mem = kb.upsert(_mem("k2"))
    kb.stage_entity_candidate(mem.id, "people", "Avery Quinn")
    with kb._pool.connection() as conn:
        conn.execute(
            "UPDATE entity_candidates SET promoted = true WHERE memory_id = %s", (mem.id,))
    assert kb.backfill_alias_links() == 0
    assert kb.memories_linked(survivor.node.id, link_type=MENTIONS) == []


def test_backfill_alias_links_skips_inactive_instance(kb):
    # An inactive/blocked instance must not receive a backfilled link — only ACTIVE instances.
    survivor = kb.upsert_entity("Avery Quinn", aliases=["Avery"])
    with kb._pool.connection() as conn:
        conn.execute(
            "UPDATE entities SET status = 'blocked' WHERE node_id = %s", (survivor.node.id,))
    mem = kb.upsert(_mem("k3"))
    kb.stage_entity_candidate(mem.id, "people", "Avery")
    with kb._pool.connection() as conn:
        conn.execute(
            "UPDATE entity_candidates SET promoted = true WHERE memory_id = %s", (mem.id,))
    assert kb.backfill_alias_links() == 0
