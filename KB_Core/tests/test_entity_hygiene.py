"""Hygiene planner/applier: typo folds, explicit ops, kind backfill — preview-first.

`plan_hygiene` is the read-only half (what WOULD change) and MUST NOT write to the DB —
the CLI's default (no --apply) runs this straight against production, so its purity is
the whole safety story. `apply_hygiene` executes an already-computed plan.
"""
from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS
from kb_core.entity_hygiene import apply_hygiene, plan_hygiene


def _mem(kb, eid):
    return kb.upsert(NodeRecord(source="agent", external_id=eid, type="note", content="c",
                                 slug=eid))


def _snapshot(kb):
    """Full (name, status, kind, entity_type) snapshot of the entities table, order-independent —
    used to prove a call made zero writes."""
    with kb._pool.connection() as conn:
        rows = conn.execute(
            "SELECT canonical_name, status, kind, entity_type FROM entities "
            "ORDER BY canonical_name, entity_type"
        ).fetchall()
    return [dict(r) for r in rows]


def test_plan_finds_typo_fold_and_kind_backfill(kb):
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=False)
    # upsert_entity_class auto-seeds well-known kinds at creation time, so
    # "company" already has kind='organization' the moment it's created. Simulate the
    # realistic case this backfill step targets: a class created before the kind axis
    # existed, sitting with kind IS NULL.
    with kb._pool.connection() as conn:
        conn.execute("UPDATE entities SET kind = NULL WHERE canonical_name = 'company'")
    plan = plan_hygiene(kb)
    assert ("conpany", "company") in plan["class_merges"]
    assert any(name == "company" and kind == "organization"
               for name, kind in plan["kind_backfills"])


def test_plan_is_pure_preview(kb):
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=False)
    plan_hygiene(kb)
    assert kb.get_entity_class("conpany") is not None      # nothing changed
    assert kb.get_entity_class("conpany").status == "inactive"


def test_plan_hygiene_makes_zero_writes(kb):
    """The read-only guarantee that makes it safe to preview against the live DB: no matter
    what the plan finds (typo folds, explicit merges/blocks, kind backfills), computing it
    never touches a row. Rowcount AND full row content must be identical before/after."""
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=False)
    kb.upsert_entity_class("icompany", active=False)
    kb.upsert_entity_class("database-column", active=False)
    with kb._pool.connection() as conn:
        conn.execute("UPDATE entities SET kind = NULL WHERE canonical_name = 'company'")

    before_rowcount = len(_snapshot(kb))
    before = _snapshot(kb)
    assert before_rowcount == 4

    plan = plan_hygiene(kb, merge=[("icompany", "company")], block=["database-column"])
    # sanity: the plan actually found something (a no-op plan proves nothing about purity)
    assert plan["class_merges"]
    assert plan["class_blocks"]
    assert plan["kind_backfills"]

    after = _snapshot(kb)
    assert len(after) == before_rowcount
    assert after == before
    assert [r["canonical_name"] for r in after] == \
        ["company", "conpany", "database-column", "icompany"]


def test_apply_executes_plan(kb):
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=False)
    plan = plan_hygiene(kb)
    result = apply_hygiene(kb, plan)
    assert result["class_merges"] == 1
    # merge_classes tombstones the source as blocked (see test_class_curation.py:
    # test_merge_folds_members_and_tombstones_source) — it doesn't delete the row.
    merged = kb.get_entity_class("conpany")
    assert merged is not None
    assert merged.status == "blocked"
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT kind FROM entities WHERE entity_type='class' AND canonical_name='company'"
        ).fetchone()
    assert row["kind"] == "organization"


def test_explicit_block(kb):
    kb.upsert_entity_class("database-column", active=False)
    plan = plan_hygiene(kb, block=["database-column"])
    result = apply_hygiene(kb, plan)
    assert result["class_blocks"] == 1
    assert kb.get_entity_class("database-column").status == "blocked"


def test_explicit_merge_fold_direction_prefers_established_target(kb):
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("icompany", active=False)
    plan = plan_hygiene(kb, merge=[("icompany", "company")])
    assert ("icompany", "company") in plan["class_merges"]
    result = apply_hygiene(kb, plan)
    assert result["class_merges"] == 1
    assert kb.get_entity_class("icompany").status == "blocked"
    assert kb.get_entity_class("company").status == "active"


def test_typo_fold_direction_folds_into_the_established_class(kb):
    """Both directions are symmetric edit-distance matches (each thinks the other is the
    typo) — the planner must pick ONE deterministic direction: the inactive/less-supported
    name folds into the active/more-supported one, never the reverse."""
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=False)
    plan = plan_hygiene(kb)
    assert ("conpany", "company") in plan["class_merges"]
    assert ("company", "conpany") not in plan["class_merges"]
    assert len(plan["class_merges"]) == 1


def test_explicit_merge_of_nonexistent_class_does_not_crash(kb):
    kb.upsert_entity_class("company", active=True)
    plan = plan_hygiene(kb, merge=[("ghost-class", "company")])
    assert plan["class_merges"] == []
    assert any("ghost-class" in w for w in plan["warnings"])
    result = apply_hygiene(kb, plan)
    assert result["class_merges"] == 0


def test_explicit_block_of_nonexistent_class_does_not_crash(kb):
    plan = plan_hygiene(kb, block=["never-existed"])
    assert plan["class_blocks"] == []
    assert any("never-existed" in w for w in plan["warnings"])
    result = apply_hygiene(kb, plan)
    assert result["class_blocks"] == 0


def test_explicit_merge_self_is_rejected(kb):
    kb.upsert_entity_class("company", active=True)
    plan = plan_hygiene(kb, merge=[("company", "company")])
    assert plan["class_merges"] == []
    assert any("company" in w for w in plan["warnings"])


def test_explicit_merge_of_nonexistent_target_warns_but_still_merges(kb):
    """`--merge-class conpany=compnay` with `compnay` never existing is NOT rejected —
    merge_classes's rename semantics (`upsert_entity_class(target, active=True)` before the
    fold) mean this legitimately creates `compnay` as a new active class. The plan must still
    include it (do not block a legitimate rename) but warn loudly, so a genuine typo in the
    target arg is visible in the preview rather than silently minting a new category."""
    kb.upsert_entity_class("conpany", active=True)
    plan = plan_hygiene(kb, merge=[("conpany", "compnay")])
    assert ("conpany", "compnay") in plan["class_merges"]
    assert any("compnay" in w and "does not exist" in w and "CREATE" in w
               for w in plan["warnings"])
    result = apply_hygiene(kb, plan)
    assert result["class_merges"] == 1
    new_cls = kb.get_entity_class("compnay")
    assert new_cls is not None
    assert new_cls.status == "active"


def test_explicit_merge_target_scheduled_for_block_warns(kb):
    """The merge-before-block ordering in apply_hygiene is unchanged — 'conpany' folds into
    'company' and THEN 'company' gets tombstoned — but that sequence must be surfaced in the
    preview rather than only discoverable after the fact."""
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=False)
    plan = plan_hygiene(kb, merge=[("conpany", "company")], block=["company"])
    assert ("conpany", "company") in plan["class_merges"]
    assert "company" in plan["class_blocks"]
    assert any("company" in w and "scheduled to be blocked" in w and "conpany" in w
               for w in plan["warnings"])


def test_plan_filters_already_blocked_explicit_target_with_warning(kb):
    """Preview must not list an already-blocked class as a pending block action — it's not
    something this run would change."""
    kb.upsert_entity_class("database-column", active=False)
    kb.block_class("database-column")
    plan = plan_hygiene(kb, block=["database-column"])
    assert plan["class_blocks"] == []
    assert any("database-column" in w and "already blocked" in w for w in plan["warnings"])
    result = apply_hygiene(kb, plan)
    assert result["class_blocks"] == 0
    assert result["class_blocks_noop"] == 0    # plan filtered it before apply ever saw it


def test_apply_hygiene_counts_already_blocked_target_as_noop_not_done(kb):
    """Defensive check at the apply_hygiene level (independent of plan_hygiene's filtering,
    e.g. a hand-built or stale plan): calling block_class on an already-blocked class must not
    inflate class_blocks — it's counted separately as a no-op."""
    kb.upsert_entity_class("database-column", active=False)
    kb.block_class("database-column")
    fake_plan = {"class_merges": [], "class_blocks": ["database-column"],
                 "kind_backfills": [], "warnings": []}
    result = apply_hygiene(kb, fake_plan)
    assert result["class_blocks"] == 0
    assert result["class_blocks_noop"] == 1


def test_apply_then_replan_is_empty(kb):
    """Round-trip idempotence: once a plan is applied, re-planning against the resulting state
    must find nothing left to do — no leftover merges, no leftover kind backfills, and no
    warnings about either. This is the guarantee that makes the CLI safe to re-run to
    convergence rather than a one-shot script."""
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=False)
    with kb._pool.connection() as conn:
        conn.execute("UPDATE entities SET kind = NULL WHERE canonical_name = 'company'")

    plan = plan_hygiene(kb)
    assert plan["class_merges"]        # sanity: there's something to apply
    assert plan["kind_backfills"]

    apply_hygiene(kb, plan)

    replan = plan_hygiene(kb)
    assert replan["class_merges"] == []
    assert replan["kind_backfills"] == []
    assert not any("merge" in w.lower() or "backfill" in w.lower() for w in replan["warnings"])


def test_explicit_merge_into_blocked_tombstone_warns_but_still_merges(kb):
    """A merge target that's already a blocked tombstone is a dead end — the source would fold
    into an invisible class. The plan must still include it (don't block it) but warn loudly
    in the preview so the operator sees it."""
    kb.upsert_entity_class("company", active=True)
    kb.block_class("company", purge_instances=False)
    kb.upsert_entity_class("conpany", active=True)
    plan = plan_hygiene(kb, merge=[("conpany", "company")])
    assert ("conpany", "company") in plan["class_merges"]
    assert any("blocked tombstone" in w and "conpany" in w and "company" in w
               for w in plan["warnings"])


# ---------------------------------------------------------------------------
# Operator visibility: plan_hygiene must
# report the expected re-enrichment wave size for each planned merge/block — the memory
# population `merge_classes`/`block_class` will requeue — so an operator sees the blast radius
# BEFORE running --apply, not just after. apply_hygiene reports the ACTUAL totals afterward.
# ---------------------------------------------------------------------------

def test_plan_reports_merge_wave_size(kb):
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=True)
    inst = kb.upsert_entity("Acme", categories=["conpany"])
    m1 = _mem(kb, "n1")
    m2 = _mem(kb, "n2")
    kb.link_entity(m1.id, inst.node.id, MENTIONS)
    kb.link_entity(m2.id, inst.node.id, MENTIONS)

    plan = plan_hygiene(kb, merge=[("conpany", "company")])

    assert plan["merge_wave_sizes"].get("conpany") == 2


def test_plan_reports_block_wave_size(kb):
    kb.upsert_entity_class("database-column", active=True)
    for eid in ("n3", "n4", "n5"):
        kb.stage_entity_candidate(_mem(kb, eid).id, "class", "database-column")

    plan = plan_hygiene(kb, block=["database-column"])

    assert plan["block_wave_sizes"].get("database-column") == 3


def test_plan_wave_sizes_are_read_only(kb):
    """Computing the wave-size counts must not write anything — same purity contract as the
    rest of plan_hygiene."""
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=True)
    inst = kb.upsert_entity("Acme", categories=["conpany"])
    kb.link_entity(_mem(kb, "n6").id, inst.node.id, MENTIONS)
    before = _snapshot(kb)
    plan_hygiene(kb, merge=[("conpany", "company")])
    after = _snapshot(kb)
    assert before == after


def test_apply_reports_actual_reenrichment_totals(kb):
    kb.upsert_entity_class("company", active=True)
    kb.upsert_entity_class("conpany", active=True)
    inst = kb.upsert_entity("Acme", categories=["conpany"])
    kb.link_entity(_mem(kb, "n7").id, inst.node.id, MENTIONS)
    kb.link_entity(_mem(kb, "n8").id, inst.node.id, MENTIONS)

    kb.upsert_entity_class("database-column", active=True)
    for eid in ("n9", "n10"):
        kb.stage_entity_candidate(_mem(kb, eid).id, "class", "database-column")

    plan = plan_hygiene(kb, merge=[("conpany", "company")], block=["database-column"])
    result = apply_hygiene(kb, plan)

    assert result["merge_reenrich_total"] == 2
    assert result["block_reenrich_total"] == 2


# ---------------------------------------------------------------------------
# backfill_alias_links wiring: plan_hygiene reports a read-only count of links that
# WOULD be restored; apply_hygiene actually restores them.
# ---------------------------------------------------------------------------

def test_plan_reports_alias_links_missing(kb):
    survivor = kb.upsert_entity("Avery Quinn", aliases=["Avery"])
    mem = _mem(kb, "n11")
    kb.stage_entity_candidate(mem.id, "people", "Avery")
    with kb._pool.connection() as conn:
        conn.execute(
            "UPDATE entity_candidates SET promoted = true WHERE memory_id = %s", (mem.id,))

    plan = plan_hygiene(kb)

    assert plan["alias_links_missing"] == 1
    assert kb.memories_linked(survivor.node.id, link_type=MENTIONS) == []   # still a preview


def test_apply_backfills_alias_links(kb):
    survivor = kb.upsert_entity("Avery Quinn", aliases=["Avery"])
    mem = _mem(kb, "n12")
    kb.stage_entity_candidate(mem.id, "people", "Avery")
    with kb._pool.connection() as conn:
        conn.execute(
            "UPDATE entity_candidates SET promoted = true WHERE memory_id = %s", (mem.id,))

    plan = plan_hygiene(kb)
    result = apply_hygiene(kb, plan)

    assert result["alias_links_backfilled"] == 1
    mentioners = {n.id for n in kb.memories_linked(survivor.node.id, link_type=MENTIONS)}
    assert mem.id in mentioners
