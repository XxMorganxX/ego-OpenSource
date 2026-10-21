import threading

import pytest
from kb_core.domain import NodeRecord, NodePatch
from kb_core.identity import node_id
from kb_core.knowledge_base import PathConflict


def _rec(**over):
    base = dict(source="agent", external_id="m1", type="note",
                content="hello world", slug="hello", parent_path="")
    base.update(over)
    return NodeRecord(**base)


def test_upsert_root_node(kb):
    node = kb.upsert(_rec())
    assert node.id == node_id("agent", "m1")
    assert node.path == "hello"
    assert node.parent_id is None


def test_upsert_creates_missing_directories(kb):
    node = kb.upsert(_rec(external_id="m2", slug="design", parent_path="projects.kb_mcp"))
    assert node.path == "projects.kb_mcp.design"
    projects = kb.get_by_path("projects")
    kb_dir = kb.get_by_path("projects.kb_mcp")
    assert projects is not None and projects.type == "folder"
    assert kb_dir is not None and kb_dir.parent_id == projects.id
    assert node.parent_id == kb_dir.id


def test_upsert_is_idempotent(kb, pool):
    kb.upsert(_rec())
    again = kb.upsert(_rec(content="updated"))
    assert again.content == "updated"
    with pool.connection() as conn:
        count = conn.execute(
            "SELECT count(*) AS c FROM nodes WHERE path = 'hello'::ltree"
        ).fetchone()["c"]
    assert count == 1


def test_source_timestamps_only_move_on_content_change(kb, pool):
    """Hash-gated source timestamps: a re-sync with unchanged content (a bulk file
    operation that only touched mtimes) must NOT rewrite authorship history, or it
    would flatten every note's source_updated_at."""
    from datetime import datetime, timezone

    t1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 5, tzinfo=timezone.utc)    # the bulk-op mtime
    t3 = datetime(2026, 7, 12, tzinfo=timezone.utc)

    def stamps():
        with pool.connection() as conn:
            return conn.execute(
                "SELECT source_created_at, source_updated_at FROM nodes "
                "WHERE path = 'hello'::ltree").fetchone()

    kb.upsert(_rec(source_created_at=t1, source_updated_at=t1))
    assert stamps()["source_updated_at"] == t1
    # same content, newer mtime — the touch must not move either timestamp
    kb.upsert(_rec(source_created_at=t2, source_updated_at=t2))
    assert stamps() == {"source_created_at": t1, "source_updated_at": t1}
    # a real content change carries the new timestamps through
    kb.upsert(_rec(content="edited", source_created_at=t1, source_updated_at=t3))
    assert stamps() == {"source_created_at": t1, "source_updated_at": t3}


def test_unchanged_resync_does_not_rewrite_the_row(kb, pool):
    """A re-sync that changes nothing must be a true no-op at the storage layer:
    no new row version (xmin stable), no updated_at bump. The obsidian period-0
    loop re-upserts the whole vault every tick; unconditional rewrites churn dead
    tuples and can corrupt the vector index."""

    def row_state():
        with pool.connection() as conn:
            return conn.execute(
                "SELECT xmin::text AS xmin, updated_at FROM nodes "
                "WHERE path = 'hello'::ltree").fetchone()

    first = kb.upsert(_rec())
    before = row_state()
    again = kb.upsert(_rec())          # identical record, e.g. a heartbeat re-sync
    after = row_state()
    assert after == before             # same tuple version, same updated_at
    # the no-op path must still return the full, correct node
    assert again.id == first.id
    assert again.content == "hello world"
    assert again.path == "hello"


def test_non_content_field_changes_still_propagate(kb, pool):
    """The no-op gate compares every synced field, not just the content hash:
    tags and event_at are outside content_hash and must still flow through."""
    from datetime import datetime, timezone

    kb.upsert(_rec(tags=["a"]))
    tagged = kb.upsert(_rec(tags=["a", "b"]))
    assert tagged.tags == ["a", "b"]

    when = datetime(2026, 5, 1, tzinfo=timezone.utc)
    dated = kb.upsert(_rec(tags=["a", "b"], event_at=when))
    assert dated.event_at == when
    with pool.connection() as conn:
        stored = conn.execute(
            "SELECT tags, event_at FROM nodes WHERE path = 'hello'::ltree").fetchone()
    assert stored == {"tags": ["a", "b"], "event_at": when}


def test_unchanged_importance_recompute_does_not_rewrite_rows(kb, pool):
    """RecomputeImportance runs after every pull tick (obsidian: period 0). When the
    computed importance matches what is stored, the per-row UPDATE must skip — this
    was the other half of that churn: every unchanged row was rewritten every tick."""
    node = kb.upsert(_rec())
    kb.recompute_importance(source="agent")     # settle stored values first

    def xmin():
        with pool.connection() as conn:
            return conn.execute(
                "SELECT xmin::text AS x FROM nodes WHERE id = %s", (node.id,)
            ).fetchone()["x"]

    before = xmin()
    examined = kb.recompute_importance(source="agent")   # nothing changed since
    assert examined >= 1                                  # node still examined
    assert xmin() == before                               # but not rewritten


def test_set_entity_eligible_false_retracts_influence_but_keeps_the_memory(kb):
    # A memory that has cast a vote and formed an entity link...
    mem = kb.upsert(_rec(external_id="noise1", slug="noise1"))
    cls = kb.upsert_entity_class("people")                       # active class
    kb.stage_entity_candidate(mem.id, "people", "Avery")        # a vote
    kb.link_entity(mem.id, cls.node.id, "belongs_to")            # an interaction
    assert kb.entities_of(mem.id)                                # link exists
    assert kb.promotable_entities(threshold=1)                   # vote exists

    # ...is flagged noise: its influence is retracted, but it stays a normal, searchable memory.
    kb.set_entity_eligible(mem.id, False, reason="random characters")
    got = kb.get(mem.id)
    assert got is not None and got.entity_eligible is False
    assert got.entity_skip_reason == "random characters"
    assert kb.entities_of(mem.id) == []                          # link retracted
    assert kb.promotable_entities(threshold=1) == []             # vote retracted
    assert mem.id in [n.id for n in kb.list()]                   # still listed


def test_set_entity_eligible_false_collects_an_orphaned_instance(kb):
    # an instance supported only by this one memory...
    mem = kb.upsert(_rec(external_id="solo", slug="solo"))
    kb.stage_entity_candidate(mem.id, "people", "Avery")
    kb.promote_entity("people", "Avery")
    assert [e.canonical_name for e in kb.instances_of("people")] == ["Avery"]
    # ...is left with zero support when the memory is flagged noise -> collected inline.
    kb.set_entity_eligible(mem.id, False, reason="noise")
    assert kb.instances_of("people") == []


def test_re_enabling_eligibility_clears_the_reason(kb):
    mem = kb.upsert(_rec(external_id="noise2", slug="noise2"))
    kb.set_entity_eligible(mem.id, False, reason="noise")
    assert kb.get(mem.id).entity_skip_reason == "noise"
    kb.set_entity_eligible(mem.id, True)                         # reason stored only while ineligible
    got = kb.get(mem.id)
    assert got.entity_eligible is True and got.entity_skip_reason is None


def test_mark_dead_keeps_influence_then_revive_restores(kb):
    # A dead (retired) memory keeps the influence it already cast — unlike noise, nothing is
    # retracted; it is merely frozen.
    cls = kb.upsert_entity_class("people")
    mem = kb.upsert(_rec(external_id="dead1", slug="dead1"))
    kb.stage_entity_candidate(mem.id, "people", "Sam")
    kb.link_entity(mem.id, cls.node.id, "belongs_to")
    assert kb.get(mem.id).lifecycle == "active"

    kb.mark_dead(mem.id, reason="event has passed")
    got = kb.get(mem.id)
    assert got.lifecycle == "dead" and got.dead_at is not None
    assert got.dead_reason == "event has passed"
    assert kb.entities_of(mem.id)                        # link KEPT
    assert kb.promotable_entities(threshold=1)           # vote KEPT

    first_death = got.dead_at
    kb.mark_dead(mem.id, reason="re-stamped")            # COALESCE keeps the original death time
    assert kb.get(mem.id).dead_at == first_death

    kb.revive(mem.id)
    got = kb.get(mem.id)
    assert got.lifecycle == "active" and got.dead_at is None and got.dead_reason is None


def test_list_excluded_separates_noise_and_dead(kb):
    kb.upsert(_rec(external_id="ex_active", slug="ex_active"))    # stays active -> not listed
    noise = kb.upsert(_rec(external_id="ex_noise", slug="ex_noise"))
    dead = kb.upsert(_rec(external_id="ex_dead", slug="ex_dead"))
    kb.set_entity_eligible(noise.id, False, reason="noise")
    kb.mark_dead(dead.id, reason="stale")

    assert {n.id for n in kb.list_excluded()} == {noise.id, dead.id}   # all shut-off, no actives
    assert {n.lifecycle for n in kb.list_excluded()} == {"noise", "dead"}
    assert [n.id for n in kb.list_excluded(kind="noise")] == [noise.id]
    assert [n.id for n in kb.list_excluded(kind="dead")] == [dead.id]


def test_dead_memory_is_frozen_from_the_reanalysis_crawl(kb, pool):
    mem = kb.upsert(_rec(external_id="crawl1", slug="crawl1"))
    with pool.connection() as conn:        # mark enriched without advancing the analyze cursor
        conn.execute("UPDATE nodes SET enrichment_status = 'done' WHERE id = %s", (mem.id,))
    hashes = {"class_hash": kb.active_class_hash(), "kind_hash": kb.doc_kind_vocab_hash()}
    due_ids = [n.id for n, _ in kb.due_for_reanalysis(["agent"], **hashes)]
    assert mem.id in due_ids                                            # crawlable while alive
    kb.mark_dead(mem.id, reason="stale")
    due_ids = [n.id for n, _ in kb.due_for_reanalysis(["agent"], **hashes)]
    assert mem.id not in due_ids                                        # frozen once dead


def test_get_returns_none_for_missing(kb):
    assert kb.get(node_id("agent", "nope")) is None


def test_get_by_path_returns_none_for_missing(kb):
    assert kb.get_by_path("does.not.exist") is None


def _seed_tree(kb):
    kb.upsert(_rec(external_id="a", slug="a", parent_path=""))
    kb.upsert(_rec(external_id="b", slug="b", parent_path="a"))
    kb.upsert(_rec(external_id="c", slug="c", parent_path="a.b"))


def test_children(kb):
    _seed_tree(kb)
    children = kb.children("a")
    assert [n.slug for n in children] == ["b"]


def test_subtree_includes_self_and_descendants(kb):
    _seed_tree(kb)
    paths = sorted(n.path for n in kb.subtree("a"))
    assert paths == ["a", "a.b", "a.b.c"]


def test_ancestors_are_strict_and_ordered(kb):
    _seed_tree(kb)
    assert [n.path for n in kb.ancestors("a.b.c")] == ["a", "a.b"]


def test_search_matches_content(kb):
    kb.upsert(_rec(external_id="s1", slug="alpha", content="postgres ltree indexing"))
    kb.upsert(_rec(external_id="s2", slug="beta", content="totally unrelated"))
    results = kb.search("ltree")
    assert len(results) == 1
    assert results[0].node.slug == "alpha"
    assert results[0].rank > 0


def test_search_scoped_to_subtree(kb):
    kb.upsert(_rec(external_id="p1", slug="note", parent_path="projects", content="shared keyword"))
    kb.upsert(_rec(external_id="e1", slug="note", parent_path="emails", content="shared keyword"))
    results = kb.search("keyword", under="projects")
    assert [r.node.path for r in results] == ["projects.note"]


def test_list_filters_by_type(kb):
    kb.upsert(_rec(external_id="l1", slug="x", parent_path="dir"))  # creates a 'folder' too
    notes = kb.list(type="note")
    assert all(n.type == "note" for n in notes)
    assert any(n.slug == "x" for n in notes)


def test_list_and_subtree_can_skip_the_body(kb):
    # Metadata listings (the dashboard tree) must not drag every note body out of TOAST:
    # with_content=False blanks content/summary and leaves every other field as-is.
    kb.upsert(_rec(external_id="b1", slug="big", parent_path="dir", content="x" * 5000,
                   title="Big", tags=["t"]))
    for full, lean in ((kb.list(under="dir"), kb.list(under="dir", with_content=False)),
                       (kb.subtree("dir"), kb.subtree("dir", with_content=False))):
        assert [n.path for n in lean] == [n.path for n in full] == ["dir", "dir.big"]
        big_full = next(n for n in full if n.slug == "big")
        big_lean = next(n for n in lean if n.slug == "big")
        assert big_full.content == "x" * 5000
        assert big_lean.content == "" and big_lean.summary is None
        assert big_lean.content_hash == big_full.content_hash
        assert (big_lean.title, big_lean.tags, big_lean.importance, big_lean.updated_at) == \
            (big_full.title, big_full.tags, big_full.importance, big_full.updated_at)


def test_update_changes_content_and_hash(kb):
    node = kb.upsert(_rec(external_id="u1", slug="u"))
    updated = kb.update(node.id, NodePatch(content="brand new body"))
    assert updated.content == "brand new body"
    assert updated.content_hash != node.content_hash


def test_move_relocates_subtree(kb):
    kb.upsert(_rec(external_id="mv1", slug="parent", parent_path="old"))
    child = kb.upsert(_rec(external_id="mv2", slug="child", parent_path="old.parent"))
    moved = kb.move(kb.get_by_path("old.parent").id, "new")
    assert moved.path == "new.parent"
    assert kb.get(child.id).path == "new.parent.child"


def test_upsert_rejects_duplicate_slug_under_same_parent(kb):
    # A *different* node (distinct external_id -> distinct id) landing on a (parent, slug) that's
    # already taken is a real collision, not an idempotent re-sync: surface it as PathConflict.
    kb.upsert(_rec(external_id="dup1", slug="tasks", parent_path="proj"))
    with pytest.raises(PathConflict):
        kb.upsert(_rec(external_id="dup2", slug="tasks", parent_path="proj"))


def test_upsert_rejects_duplicate_slug_at_root(kb):
    # Root nodes have parent_id = NULL (distinct), so a root collision trips UNIQUE(path) instead;
    # it must still translate to PathConflict, not leak the raw violation.
    kb.upsert(_rec(external_id="r1", slug="tasks", parent_path=""))
    with pytest.raises(PathConflict):
        kb.upsert(_rec(external_id="r2", slug="tasks", parent_path=""))


def test_move_onto_occupied_slug_is_rejected(kb):
    kb.upsert(_rec(external_id="o1", slug="tasks", parent_path="dst"))   # dst.tasks taken
    src = kb.upsert(_rec(external_id="o2", slug="tasks", parent_path="src"))
    with pytest.raises(PathConflict):
        kb.move(src.id, "dst")


def test_delete_leaf(kb):
    node = kb.upsert(_rec(external_id="d1", slug="leaf"))
    assert kb.delete(node.id) == 1
    assert kb.get(node.id) is None


def test_delete_nonrecursive_with_children_is_rejected(kb):
    kb.upsert(_rec(external_id="d2", slug="dir2", parent_path="container"))
    container = kb.get_by_path("container")
    with pytest.raises(Exception):
        kb.delete(container.id)


def test_delete_recursive_removes_subtree(kb):
    kb.upsert(_rec(external_id="d3", slug="x", parent_path="root3.sub"))
    removed = kb.delete(kb.get_by_path("root3").id, recursive=True)
    assert removed == 3  # root3, root3.sub, root3.sub.x
    assert kb.get_by_path("root3") is None


def test_update_rejects_empty_content(kb):
    node = kb.upsert(_rec(external_id="u2", slug="u2"))
    with pytest.raises(Exception):
        kb.update(node.id, NodePatch(content="   "))


def test_update_does_not_lose_a_concurrent_writers_field(kb):
    """update() used to be self.get(id) on one connection, a Python-side merge, then a
    second, separate connection for the write -- move/delete/upsert are all
    single-transaction; update was the outlier. Two concurrent PARTIAL patches to the
    SAME node (A sets summary, B sets tags) used to both read the same pre-write
    snapshot, so whichever wrote SECOND clobbered the other's field back to its stale
    value -- a lost update. Unreachable while MCP tools ran on the event loop;
    moving them to worker threads removed that in-process serialisation.

    Deterministic construction, no sleep-based race: a raw connection takes `FOR
    UPDATE` on the row and holds it, forcing a REAL, concurrent kb.update(...) call to
    block on its own read. The raw connection then performs (and commits) A's write
    -- summary -- while B is still blocked, before B's read ever proceeds. If update()
    merges against a FRESH read (the fix), B's patch (tags) lands on top of A's
    already-committed summary. If it merges against a STALE one (pre-fix), B's write
    silently reverts A's summary to what it was before A ever wrote it."""
    node = kb.upsert(_rec(external_id="lost-update", slug="lu", summary="orig",
                          tags=["a"]))

    with kb._pool.connection() as holder:
        row = holder.execute(
            "SELECT id FROM nodes WHERE id = %s FOR UPDATE", (node.id,)).fetchone()
        assert row is not None            # lock acquired, transaction still open

        result: dict = {}

        def _concurrent_update():
            result["node"] = kb.update(node.id, NodePatch(tags=["b"]))

        t = threading.Thread(target=_concurrent_update)
        t.start()
        t.join(timeout=0.5)
        assert t.is_alive(), (
            "kb.update did not block on the held row lock -- FOR UPDATE regressed")

        # A's write, made and committed while B is still blocked on its own read.
        holder.execute(
            "UPDATE nodes SET summary = %s, updated_at = now() WHERE id = %s",
            ("changed-by-a", node.id))
        holder.commit()

        t.join(timeout=5)
        assert not t.is_alive()

    # B's patch applied AND A's write survived -- neither writer lost a field.
    assert result["node"].tags == ["b"]
    assert result["node"].summary == "changed-by-a"
    assert kb.get(node.id).summary == "changed-by-a"


def test_move_into_own_subtree_is_rejected(kb):
    kb.upsert(_rec(external_id="cyc", slug="x", parent_path="a.b"))  # creates a, a.b, a.b.x
    a = kb.get_by_path("a")
    with pytest.raises(Exception):
        kb.move(a.id, "a.b")


def test_external_ids_for_source(kb):
    kb.upsert(_rec(source="obsidian", external_id="a", slug="a"))
    kb.upsert(_rec(source="obsidian", external_id="b", slug="b"))
    assert kb.external_ids_for_source("obsidian") == {"a", "b"}


def test_prune_deletes_missing_keeps_kept(kb):
    kb.upsert(_rec(source="obsidian", external_id="a", slug="a"))
    kb.upsert(_rec(source="obsidian", external_id="b", slug="b"))
    assert kb.prune("obsidian", {"a"}) == 1
    assert kb.external_ids_for_source("obsidian") == {"a"}


def test_prune_empty_keep_deletes_all_of_source(kb):
    kb.upsert(_rec(source="obsidian", external_id="a", slug="a"))
    assert kb.prune("obsidian", set()) == 1


def test_prune_ignores_other_sources(kb):
    kb.upsert(_rec(source="obsidian", external_id="a", slug="a"))
    kb.upsert(_rec(source="agent", external_id="z", slug="z"))
    kb.prune("obsidian", set())
    assert kb.external_ids_for_source("agent") == {"z"}


def test_upsert_returns_importance_defaults(kb):
    node = kb.upsert(_rec(external_id="imp1", slug="imp1"))
    assert node.importance == 1.0
    assert node.importance_modifiers == {}
    assert node.access_count == 0
    assert node.last_accessed_at is None


def test_set_importance_raises_and_records_manual(kb):
    node = kb.upsert(_rec(external_id="si1", slug="si1"))
    updated = kb.set_importance(node.id, 1.0)
    assert updated.importance == 2.0
    assert updated.importance_modifiers["manual"] == 1.0


def test_recompute_folds_access_count(kb, pool):
    node = kb.upsert(_rec(external_id="si2", slug="si2"))
    with pool.connection() as conn:
        conn.execute("UPDATE nodes SET access_count = 10 WHERE id = %s", (node.id,))
    kb.recompute_importance(ids=[node.id])
    assert kb.get(node.id).importance == 1.0 + 0.5 * (10 / 20)   # 1.25


def test_reupsert_preserves_importance(kb):
    node = kb.upsert(_rec(external_id="si3", slug="si3"))
    kb.set_importance(node.id, 2.0)
    again = kb.upsert(_rec(external_id="si3", slug="si3", content="changed"))
    assert again.importance == 3.0                     # manual 2.0 preserved -> 1 + 2
    assert again.importance_modifiers["manual"] == 2.0


def test_set_importance_raises_on_missing(kb):
    import pytest
    from kb_core.identity import node_id
    with pytest.raises(LookupError):
        kb.set_importance(node_id("agent", "nope"), 1.0)


def test_recompute_all_nodes(kb, pool):
    a = kb.upsert(_rec(external_id="ra1", slug="ra1"))
    b = kb.upsert(_rec(external_id="ra2", slug="ra2"))
    with pool.connection() as conn:
        conn.execute("UPDATE nodes SET access_count = 10 WHERE id = ANY(%s)", ([a.id, b.id],))
    count = kb.recompute_importance()              # no scope = all nodes
    assert count >= 2
    assert kb.get(a.id).importance == 1.25
    assert kb.get(b.id).importance == 1.25


def test_search_bumps_access_and_returns_score(kb):
    kb.upsert(_rec(external_id="se1", slug="se1", content="alpha topic"))
    results = kb.search("alpha")
    assert len(results) == 1
    assert results[0].score > 0
    assert kb.get(results[0].node.id).access_count == 1     # search recorded an access


def test_get_track_access_flag(kb):
    node = kb.upsert(_rec(external_id="se2", slug="se2"))
    kb.get(node.id)                       # default: no tracking
    assert kb.get(node.id).access_count == 0
    kb.get(node.id, track_access=True)
    assert kb.get(node.id).access_count == 1


def test_importance_outranks_relevance(kb):
    kb.upsert(_rec(external_id="hi", slug="hi", content="alpha alpha alpha alpha"))  # higher ts_rank
    low = kb.upsert(_rec(external_id="lo", slug="lo", content="alpha"))             # lower ts_rank
    kb.set_importance(low.id, 4.0)        # importance 5.0 -> should win
    results = kb.search("alpha")
    assert results[0].node.slug == "lo"


def test_list_does_not_record_access(kb):
    node = kb.upsert(_rec(external_id="se3", slug="se3"))
    kb.list()
    assert kb.get(node.id).access_count == 0


def test_upsert_returns_enrichment_defaults(kb):
    node = kb.upsert(_rec(external_id="enr1", slug="enr1"))
    assert node.enrichment_status == "none"
    assert node.enriched_at is None
    assert node.enriched_hash is None


def test_flag_stale_enrichment_flags_new_and_changed(kb):
    a = kb.upsert(_rec(external_id="fa", slug="fa", content="one"))
    assert kb.flag_stale_enrichment() >= 1
    assert kb.get(a.id).enrichment_status == "pending"
    # mark done, re-flag -> nothing changes; then change content -> re-flagged
    kb.mark_enriched(a.id)
    assert kb.flag_stale_enrichment() == 0
    kb.upsert(_rec(external_id="fa", slug="fa", content="two"))   # content_hash changes
    assert kb.flag_stale_enrichment() == 1
    assert kb.get(a.id).enrichment_status == "pending"


def test_claim_enrichment_moves_pending_to_processing(kb):
    a = kb.upsert(_rec(external_id="ca", slug="ca", content="x"))
    kb.flag_stale_enrichment()
    claimed = kb.claim_enrichment(10)
    assert [n.id for n in claimed] == [a.id]
    assert kb.get(a.id).enrichment_status == "processing"
    assert kb.claim_enrichment(10) == []                          # nothing left pending


def test_requeue_stuck_processing_recovers_stranded_claims(kb):
    # A daemon killed mid-batch strands its claimed nodes at 'processing'; nothing else
    # touches that status (flag_stale now skips it), so the ingest daemon requeues the
    # stragglers once at boot — safe there because it is the only claimer.
    a = kb.upsert(_rec(external_id="rq", slug="rq", content="x"))
    kb.flag_stale_enrichment()
    kb.claim_enrichment(10)
    assert kb.requeue_stuck_processing() == 1
    assert kb.get(a.id).enrichment_status == "pending"
    assert kb.requeue_stuck_processing() == 0          # idempotent on a clean table


def test_flag_stale_enrichment_leaves_in_flight_processing_alone(kb):
    # A source re-sync mid-batch (every pull runs FlagEnrichment) must not knock a
    # claimed node back to 'pending': the batch is still working on it, so the flip
    # only falsifies the observable state ("N pending, 0 processing" while enrichment
    # is in full flight) — mark_enriched/mark_enrichment_failed settle it at the end.
    a = kb.upsert(_rec(external_id="cp", slug="cp", content="x"))
    kb.flag_stale_enrichment()
    kb.claim_enrichment(10)
    assert kb.flag_stale_enrichment() == 0
    assert kb.get(a.id).enrichment_status == "processing"


def test_mark_enriched_sets_hash_and_done(kb):
    a = kb.upsert(_rec(external_id="me", slug="me", content="x"))
    kb.mark_enriched(a.id)
    n = kb.get(a.id)
    assert n.enrichment_status == "done"
    assert n.enriched_at is not None
    assert n.enriched_hash == n.content_hash


def test_delete_by_external_id(kb):
    kb.upsert(_rec(source="webhook", external_id="d1", slug="d1"))
    assert kb.delete_by_external_id("webhook", "d1") == 1
    assert kb.external_ids_for_source("webhook") == set()
    assert kb.delete_by_external_id("webhook", "missing") == 0
