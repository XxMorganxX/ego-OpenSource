from kb_core.domain import NodeRecord


def _rec(**over):
    base = dict(source="agent", external_id="m1", type="note",
                content="body", slug="m1", title="T")
    base.update(over)
    return NodeRecord(**base)


def test_analysis_excluded_column_exists(pool):
    with pool.connection() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'nodes'"
        ).fetchall()}
    assert "analysis_excluded" in cols
    assert "analysis_excluded_at" in cols


def test_new_node_defaults_to_not_excluded(kb):
    n = kb.upsert(_rec(external_id="a", slug="a"))
    got = kb.get(n.id)
    assert got.analysis_excluded is False
    assert got.analysis_excluded_at is None


def test_set_and_clear_own_flag_with_timestamp(kb):
    n = kb.upsert(_rec(external_id="a", slug="a"))
    excluded = kb.set_analysis_excluded(n.id, True)
    assert excluded.analysis_excluded is True
    assert excluded.analysis_excluded_at is not None
    included = kb.set_analysis_excluded(n.id, False)
    assert included.analysis_excluded is False
    assert included.analysis_excluded_at is None


def test_effective_exclusion_self_ancestor_and_not_sibling(kb):
    kb.upsert(_rec(external_id="d", slug="doc", parent_path="scratch"))
    kb.upsert(_rec(external_id="k", slug="keep", parent_path="projects"))
    folder = kb.get_by_path("scratch")
    kb.set_analysis_excluded(folder.id, True)

    assert kb.analysis_excluded(kb.get_by_path("scratch")) is True        # self
    assert kb.analysis_excluded(kb.get_by_path("scratch.doc")) is True    # via ancestor folder
    assert kb.get_by_path("scratch.doc").analysis_excluded is False       # own-flag stays unset
    assert kb.analysis_excluded(kb.get_by_path("projects.keep")) is False  # unrelated tree


def test_analysis_excluded_paths_lists_only_self_flagged(kb):
    kb.upsert(_rec(external_id="d", slug="doc", parent_path="scratch"))
    kb.upsert(_rec(external_id="k", slug="keep", parent_path="projects"))
    assert kb.analysis_excluded_paths() == []                  # nothing flagged yet
    kb.set_analysis_excluded(kb.get_by_path("scratch").id, True)
    # only the toggled folder is returned (descendants inherit by ltree prefix, not their own flag)
    assert kb.analysis_excluded_paths() == ["scratch"]


def test_exclude_and_retract_is_scoped_to_the_subtree(kb):
    cls = kb.upsert_entity_class("people")
    inside = kb.upsert(_rec(external_id="in", slug="inside", parent_path="scratch"))
    outside = kb.upsert(_rec(external_id="out", slug="outside", parent_path="projects"))
    for m in (inside, outside):
        kb.stage_entity_candidate(m.id, "people", "Sam")
        kb.link_entity(m.id, cls.node.id, "belongs_to")

    folder = kb.get_by_path("scratch")
    kb.set_analysis_excluded(folder.id, True, retract=True)

    assert kb.entities_of(inside.id) == []     # subtree memory: influence retracted
    assert kb.entities_of(outside.id)          # sibling tree untouched


def test_retract_collects_instance_orphaned_by_exclusion(kb):
    # an instance supported only by memories in the excluded subtree...
    inside = [kb.upsert(_rec(external_id=f"i{i}", slug=f"i{i}", parent_path="scratch"))
              for i in range(2)]
    for m in inside:
        kb.stage_entity_candidate(m.id, "people", "Sam")
    kb.promote_entity("people", "Sam")
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Sam"]
    # ...is left with zero support by the retract, so it is collected inline.
    kb.set_analysis_excluded(kb.get_by_path("scratch").id, True, retract=True)
    assert kb.instances_of("people") == []


def test_retract_spares_instance_still_supported_outside_subtree(kb):
    inside = kb.upsert(_rec(external_id="in", slug="in", parent_path="scratch"))
    outside = kb.upsert(_rec(external_id="out", slug="out", parent_path="projects"))
    for m in (inside, outside):
        kb.stage_entity_candidate(m.id, "people", "Sam")
    kb.promote_entity("people", "Sam")
    kb.set_analysis_excluded(kb.get_by_path("scratch").id, True, retract=True)
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Sam"]  # outside still backs it


def test_exclude_without_retract_keeps_influence(kb):
    cls = kb.upsert_entity_class("people")
    m = kb.upsert(_rec(external_id="m", slug="keep", parent_path="scratch"))
    kb.stage_entity_candidate(m.id, "people", "Sam")
    kb.link_entity(m.id, cls.node.id, "belongs_to")

    folder = kb.get_by_path("scratch")
    kb.set_analysis_excluded(folder.id, True)  # retract defaults False
    assert kb.entities_of(m.id)                # influence kept


def _done_and_stale(pool, *nids):
    with pool.connection() as conn:
        for nid in nids:
            conn.execute(
                "UPDATE nodes SET enrichment_status='done', "
                "last_analyzed_at = now() - interval '10 days' WHERE id=%s", (nid,))


def test_due_for_reanalysis_skips_excluded_subtree(kb, pool):
    keep = kb.upsert(_rec(external_id="keep", slug="keep", parent_path="projects"))
    drop = kb.upsert(_rec(external_id="drop", slug="drop", parent_path="scratch"))
    _done_and_stale(pool, keep.id, drop.id)
    kb.set_analysis_excluded(kb.get_by_path("scratch").id, True)

    due = {n.id for n, _ in kb.due_for_reanalysis(["agent"], limit=10,
                                                   class_hash=kb.active_class_hash(),
                                                   kind_hash=kb.doc_kind_vocab_hash())}
    assert keep.id in due
    assert drop.id not in due


def test_crawlable_node_returns_none_for_excluded(kb, pool):
    drop = kb.upsert(_rec(external_id="drop", slug="drop", parent_path="scratch"))
    _done_and_stale(pool, drop.id)
    kb.set_analysis_excluded(kb.get_by_path("scratch").id, True)
    assert kb.crawlable_node(drop.id, ["agent"]) is None


def test_backfill_skips_excluded_subtree(kb, pool):
    from datetime import datetime, timezone
    keep = kb.upsert(_rec(external_id="keep", slug="keep", parent_path="projects"))
    drop = kb.upsert(_rec(external_id="drop", slug="drop", parent_path="scratch"))
    with pool.connection() as conn:
        conn.execute("UPDATE nodes SET enrichment_status='done' WHERE id = ANY(%s)",
                     ([keep.id, drop.id],))
    kb.set_analysis_excluded(kb.get_by_path("scratch").id, True)
    before = datetime.now(timezone.utc)
    ids = {n.id for n in kb.due_for_backfill(["agent"], before=before, limit=10)}
    assert keep.id in ids and drop.id not in ids
