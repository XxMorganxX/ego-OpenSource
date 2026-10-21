from kb_core.domain import NodeRecord


def _rec(**over):
    base = dict(source="agent", external_id="m1", type="note",
                content="body", slug="m1", title="T")
    base.update(over)
    return NodeRecord(**base)


def _exclude_folder(kb, path):
    """Materialize `path` as a folder (via a throwaway child note) and flag it excluded."""
    kb.upsert(_rec(external_id=f"_ph:{path}", slug="_ph", parent_path=path))
    kb.set_analysis_excluded(kb.get_by_path(path).id, True)


def test_move_into_excluded_retracts_influence(kb):
    cls = kb.upsert_entity_class("people")
    _exclude_folder(kb, "completed")
    m = kb.upsert(_rec(external_id="t", slug="task", parent_path="todos"))
    kb.stage_entity_candidate(m.id, "people", "Sam")
    kb.link_entity(m.id, cls.node.id, "belongs_to")
    assert kb.entities_of(m.id)                  # influence present before the move

    kb.move(m.id, "completed")                   # todos.task -> completed.task (excluded)

    assert kb.entities_of(m.id) == []            # influence retracted


def test_move_active_to_active_keeps_influence(kb):
    cls = kb.upsert_entity_class("people")
    m = kb.upsert(_rec(external_id="t", slug="task", parent_path="todos"))
    kb.stage_entity_candidate(m.id, "people", "Sam")
    kb.link_entity(m.id, cls.node.id, "belongs_to")

    kb.move(m.id, "projects")                    # both folders active -> no-op

    assert kb.entities_of(m.id)                  # influence kept


def test_move_into_excluded_collects_orphaned_instance(kb):
    _exclude_folder(kb, "completed")
    notes = [kb.upsert(_rec(external_id=f"i{i}", slug=f"i{i}", parent_path="todos.batch"))
             for i in range(2)]
    for m in notes:
        kb.stage_entity_candidate(m.id, "people", "Sam")
    kb.promote_entity("people", "Sam")
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Sam"]

    kb.move(kb.get_by_path("todos.batch").id, "completed")   # whole subtree -> excluded

    assert kb.instances_of("people") == []       # instance left with zero support is collected


def _set_enriched(pool, *nids):
    with pool.connection() as conn:
        conn.execute(
            "UPDATE nodes SET enrichment_status='done', enriched_hash='h' WHERE id = ANY(%s)",
            (list(nids),))


def _status(pool, nid):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT enrichment_status, enriched_hash FROM nodes WHERE id = %s", (nid,)).fetchone()


def test_move_out_of_excluded_restages(kb, pool):
    _exclude_folder(kb, "completed")
    m = kb.upsert(_rec(external_id="t", slug="task", parent_path="completed"))  # excluded via ancestor
    _set_enriched(pool, m.id)

    kb.move(m.id, "todos")                       # completed.task -> todos.task (now active)

    row = _status(pool, m.id)
    assert row["enrichment_status"] == "pending"
    assert row["enriched_hash"] is None


def test_move_out_skips_nested_own_flag(kb, pool):
    _exclude_folder(kb, "completed")                                   # ancestor flag
    keep = kb.upsert(_rec(external_id="k", slug="keep", parent_path="completed.batch"))
    nested = kb.upsert(_rec(external_id="n", slug="n", parent_path="completed.batch.locked"))
    kb.set_analysis_excluded(kb.get_by_path("completed.batch.locked").id, True)  # nested own flag
    _set_enriched(pool, keep.id, nested.id)

    kb.move(kb.get_by_path("completed.batch").id, "todos")             # completed.batch -> todos.batch

    assert _status(pool, keep.id)["enrichment_status"] == "pending"    # ancestor flag lifted -> re-staged
    assert _status(pool, nested.id)["enrichment_status"] == "done"     # own flag remains -> untouched


def test_sync_reupsert_into_excluded_retracts(kb):
    cls = kb.upsert_entity_class("people")
    _exclude_folder(kb, "completed")
    m = kb.upsert(_rec(external_id="t", slug="task", parent_path="todos"))
    kb.stage_entity_candidate(m.id, "people", "Sam")
    kb.link_entity(m.id, cls.node.id, "belongs_to")
    assert kb.entities_of(m.id)

    # A vault move re-syncs the same node (same external_id) at the new, excluded path.
    kb.upsert(_rec(external_id="t", slug="task", parent_path="completed"))

    assert kb.entities_of(m.id) == []


def test_sync_reupsert_out_of_excluded_restages(kb, pool):
    _exclude_folder(kb, "completed")
    m = kb.upsert(_rec(external_id="t", slug="task", parent_path="completed"))
    _set_enriched(pool, m.id)

    kb.upsert(_rec(external_id="t", slug="task", parent_path="todos"))   # re-sync at active path

    row = _status(pool, m.id)
    assert row["enrichment_status"] == "pending"
    assert row["enriched_hash"] is None


def test_sync_reupsert_same_path_keeps_influence(kb):
    cls = kb.upsert_entity_class("people")
    m = kb.upsert(_rec(external_id="t", slug="task", parent_path="todos"))
    kb.stage_entity_candidate(m.id, "people", "Sam")
    kb.link_entity(m.id, cls.node.id, "belongs_to")

    # An ordinary content re-sync (same path) must not touch influence.
    kb.upsert(_rec(external_id="t", slug="task", parent_path="todos", content="changed"))

    assert kb.entities_of(m.id)


def test_sync_reupsert_into_excluded_collects_orphan(kb):
    _exclude_folder(kb, "completed")
    notes = [kb.upsert(_rec(external_id=f"i{i}", slug=f"i{i}", parent_path="todos"))
             for i in range(2)]
    for m in notes:
        kb.stage_entity_candidate(m.id, "people", "Sam")
    kb.promote_entity("people", "Sam")
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Sam"]

    for m in notes:
        kb.upsert(_rec(external_id=m.external_id, slug=m.slug, parent_path="completed"))

    assert kb.instances_of("people") == []
