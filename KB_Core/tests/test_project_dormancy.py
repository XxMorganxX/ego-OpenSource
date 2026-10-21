"""Project-dormancy decay of the inherited boost.

The Projects folder's manual value cascades to every mirrored repo doc as `inherited`;
_recompute_in_conn now shrinks that boost by 2^(-days/182.5) where days = whole days
since ANY note in the project last changed (event_at or hash-gated ingest event)."""
from datetime import datetime, timedelta, timezone

from kb_core.domain import NodeRecord
from kb_core.importance import dormancy_factor
from kb_core.knowledge_base import KnowledgeBase
from kb_core.policy import Policy, Projects
from kb_core.registry import default_registry


def _note(kb, ext, parent_path, **kw):
    kw.setdefault("content", f"body of {ext}")
    return kb.upsert(NodeRecord(source="obsidian", external_id=ext, type="note",
                                slug=ext.split("/")[-1], parent_path=parent_path, **kw))


def _age_ingest_events(pool, node_id, days):
    with pool.connection() as conn:
        conn.execute("UPDATE ingest_log SET at = now() - make_interval(days => %s) "
                     "WHERE node_id = %s", (days, node_id))


def _boost_projects_folder(kb, path="Obsidian.Projects"):
    """Give the Projects folder manual +1.0; cascades as inherited."""
    folder = kb.get_by_path(path)
    assert folder is not None and folder.type == "folder"
    kb.set_importance(folder.id, 1.0)


def _imp(kb, node_id):
    n = kb.get(node_id)
    return n.importance, n.importance_modifiers


def test_active_project_keeps_the_full_boost(kb):
    doc = _note(kb, "p/a/doc", "Obsidian.Projects.alpha.docs")
    _boost_projects_folder(kb)                        # recomputes the subtree
    imp, mods = _imp(kb, doc.id)
    assert imp == 2.0                                  # 1 + inherited 1.0, dormancy 0
    assert mods["inherited"] == 1.0
    assert mods["project_dormancy"] == 0.0


def test_quiet_project_loses_boost_by_half_life(kb, pool):
    doc = _note(kb, "p/b/doc", "Obsidian.Projects.beta.docs")
    _age_ingest_events(pool, doc.id, 365)             # the only activity: a year ago
    _boost_projects_folder(kb)
    imp, mods = _imp(kb, doc.id)
    expected = 1.0 + dormancy_factor(365, 182.5)              # 1.25
    assert abs(imp - expected) < 1e-6                  # importance is float4
    assert abs(mods["project_dormancy"] - (1.0 - dormancy_factor(365, 182.5))) < 1e-9


def test_any_sibling_note_activity_counts_for_the_whole_project(kb, pool):
    old = _note(kb, "p/g/old", "Obsidian.Projects.gamma.docs")
    _age_ingest_events(pool, old.id, 365)
    _note(kb, "p/g/fresh", "Obsidian.Projects.gamma.tasks")   # ingested now
    _boost_projects_folder(kb)
    imp, _ = _imp(kb, old.id)
    assert imp == 2.0                                  # project is active -> no decay


def test_event_at_counts_as_activity(kb, pool):
    doc = _note(kb, "p/d/doc", "Obsidian.Projects.delta.docs",
                event_at=datetime.now(timezone.utc) - timedelta(days=2))
    _age_ingest_events(pool, doc.id, 365)             # authored long ago, HAPPENED now
    _boost_projects_folder(kb)
    imp, _ = _imp(kb, doc.id)
    assert abs(imp - (1.0 + dormancy_factor(2, 182.5))) < 1e-6


def test_future_event_at_clamps_to_no_decay(kb, pool):
    doc = _note(kb, "p/e/doc", "Obsidian.Projects.epsilon.docs",
                event_at=datetime.now(timezone.utc) + timedelta(days=30))
    _age_ingest_events(pool, doc.id, 365)
    _boost_projects_folder(kb)
    imp, _ = _imp(kb, doc.id)
    assert imp == 2.0


def test_content_change_resets_the_clock(kb, pool):
    doc = _note(kb, "p/z/doc", "Obsidian.Projects.zeta.docs")
    _age_ingest_events(pool, doc.id, 365)
    _boost_projects_folder(kb)
    assert _imp(kb, doc.id)[0] < 1.3
    # An unchanged re-sync logs nothing and changes nothing.
    _note(kb, "p/z/doc", "Obsidian.Projects.zeta.docs")
    kb.recompute_importance(source="obsidian")
    assert _imp(kb, doc.id)[0] < 1.3
    # A real content change logs node_updated now -> full boost again.
    _note(kb, "p/z/doc", "Obsidian.Projects.zeta.docs", content="rewritten")
    kb.recompute_importance(source="obsidian")
    assert _imp(kb, doc.id)[0] == 2.0


def test_non_project_docs_and_deep_projects_label_are_untouched(kb, pool):
    # A Projects label at depth 2 is not a project subtree.
    deep = _note(kb, "m/p/x", "Obsidian.Meetings.Projects.x")
    plain = _note(kb, "n/plain", "Obsidian.Notes")
    _age_ingest_events(pool, deep.id, 365)
    _age_ingest_events(pool, plain.id, 365)
    meetings = kb.get_by_path("Obsidian.Meetings")
    kb.set_importance(meetings.id, 1.0)               # an inherited boost that must NOT decay
    kb.recompute_importance(source="obsidian")
    assert _imp(kb, deep.id)[0] == 2.0
    assert _imp(kb, deep.id)[1]["project_dormancy"] == 0.0
    assert _imp(kb, plain.id)[0] == 1.0


def test_projects_label_at_root_also_qualifies(kb, pool):
    doc = _note(kb, "r/p/doc", "Projects.rho.docs")   # no source layer
    _age_ingest_events(pool, doc.id, 365)
    _boost_projects_folder(kb, path="Projects")
    imp, _ = _imp(kb, doc.id)
    assert abs(imp - (1.0 + dormancy_factor(365, 182.5))) < 1e-6


def test_manual_boost_on_a_dormant_doc_survives(kb, pool):
    doc = _note(kb, "p/m/doc", "Obsidian.Projects.mu.docs")
    _age_ingest_events(pool, doc.id, 365)
    _boost_projects_folder(kb)
    kb.set_importance(doc.id, 1.0)                    # manual +1.0 on the doc itself
    imp, mods = _imp(kb, doc.id)
    assert abs(imp - (1.0 + 1.0 + dormancy_factor(365, 182.5))) < 1e-6
    assert mods["manual"] == 1.0


def test_same_day_recompute_is_a_storage_noop(kb, pool):
    """The value must be bit-identical across ticks within a day
    so the no-op write gate (importance::real / modifiers::jsonb) skips the UPDATE."""
    doc = _note(kb, "p/n/doc", "Obsidian.Projects.nu.docs")
    _age_ingest_events(pool, doc.id, 100)
    _boost_projects_folder(kb)
    with pool.connection() as conn:
        before = conn.execute("SELECT xmin::text AS x FROM nodes WHERE id = %s",
                              (doc.id,)).fetchone()["x"]
    kb.recompute_importance(source="obsidian")
    kb.recompute_importance(source="obsidian")
    with pool.connection() as conn:
        after = conn.execute("SELECT xmin::text AS x FROM nodes WHERE id = %s",
                             (doc.id,)).fetchone()["x"]
    assert before == after                             # no new row version


def test_project_dormant_days_helper_reports_whole_days(kb, pool):
    doc = _note(kb, "p/h/doc", "Obsidian.Projects.eta.docs")
    _age_ingest_events(pool, doc.id, 40)
    with pool.connection() as conn:
        days = kb._project_dormant_days(conn)
    assert days["Obsidian.Projects.eta"] == 40
    with pool.connection() as conn:
        only = kb._project_dormant_days(conn, keys=["Obsidian.Projects.eta"])
    assert only == {"Obsidian.Projects.eta": 40}


def test_significance_view_reports_project_dormant_days(kb, pool):
    doc = _note(kb, "p/s/doc", "Obsidian.Projects.sigma.docs")
    _age_ingest_events(pool, doc.id, 21)
    view = kb.significance_view(kb.get(doc.id))
    assert view["project_dormant_days"] == 21
    plain = _note(kb, "n/s/plain", "Obsidian.Notes")
    assert kb.significance_view(kb.get(plain.id))["project_dormant_days"] is None


def test_project_folder_carries_the_key_but_the_projects_folder_itself_does_not(kb, pool):
    """The project root folder shares its project's key (and so inherits the boost's
    decay via the under= subtree recompute), but the Projects folder itself never has a
    key — a Projects label at depth 1 with nlevel 2 fails the nlevel >= p+2 test."""
    doc = _note(kb, "p/i/doc", "Obsidian.Projects.iota.docs")
    _age_ingest_events(pool, doc.id, 365)
    _boost_projects_folder(kb)                        # recomputes the whole subtree
    with pool.connection() as conn:
        days = kb._project_dormant_days(conn)
    assert days["Obsidian.Projects.iota"] == 365

    root = kb.get_by_path("Obsidian.Projects.iota")
    assert root is not None and root.type == "folder"
    assert root.importance_modifiers["project_dormancy"] > 0

    projects_folder = kb.get_by_path("Obsidian.Projects")
    assert projects_folder.importance_modifiers["project_dormancy"] == 0.0


def test_no_activity_signal_is_none_not_full_decay(kb, pool):
    """A project note with no event_at and no ingest_log rows at all gets None ("no
    signal, no decay"), not treated as either fully fresh or fully dormant."""
    doc = _note(kb, "p/k/doc", "Obsidian.Projects.kappa.docs")
    _boost_projects_folder(kb)
    assert kb.get(doc.id).event_at is None
    with pool.connection() as conn:
        conn.execute("DELETE FROM ingest_log WHERE node_id = %s", (doc.id,))
    with pool.connection() as conn:
        days = kb._project_dormant_days(conn)
    assert days["Obsidian.Projects.kappa"] is None

    kb.recompute_importance(source="obsidian")
    imp, mods = _imp(kb, doc.id)
    assert imp == 2.0
    assert mods["project_dormancy"] == 0.0


def test_scoped_recompute_still_uses_the_project_wide_aggregate(kb, pool):
    """recompute_importance(ids=[old.id]) narrows which rows get written, not which notes
    feed the project's activity aggregate — a fresh sibling outside the scope must still
    keep the whole project (and so `old`) active."""
    old = _note(kb, "p/o/old", "Obsidian.Projects.omicron.docs")
    _age_ingest_events(pool, old.id, 365)
    _note(kb, "p/o/fresh", "Obsidian.Projects.omicron.tasks")   # ingested now, out of scope
    _boost_projects_folder(kb)
    kb.recompute_importance(ids=[old.id])
    imp, _ = _imp(kb, old.id)
    assert imp == 2.0                                  # project stays active -> no decay


def _kb_with(pool, policy):
    return KnowledgeBase(pool, default_registry(), policy=policy)


def test_no_projects_section_keeps_the_boost_and_reports_no_key(kb, pool):
    plain = _kb_with(pool, Policy())                   # shipped defaults
    doc = _note(plain, "p/z/doc", "Obsidian.Projects.zeta.docs")
    _age_ingest_events(pool, doc.id, 365)
    _boost_projects_folder(plain)
    imp, mods = _imp(plain, doc.id)
    assert imp == 2.0 and mods["project_dormancy"] == 0.0
    assert plain.significance_view(plain.get(doc.id))["project_dormant_days"] is None


def test_max_depth_zero_only_accepts_the_label_at_the_root(kb, pool):
    shallow = _kb_with(pool, Policy(projects=Projects("Projects", max_depth=0)))
    under_layer = _note(shallow, "p/y/doc", "Obsidian.Projects.eta.docs")
    at_root = _note(shallow, "p/x/doc", "Projects.theta.docs")
    for n in (under_layer, at_root):
        _age_ingest_events(pool, n.id, 365)
    _boost_projects_folder(shallow)                    # Obsidian.Projects
    _boost_projects_folder(shallow, path="Projects")
    assert _imp(shallow, under_layer.id)[0] == 2.0     # depth 1: not a project -> no decay
    assert abs(_imp(shallow, at_root.id)[0] - 1.25) < 1e-6   # depth 0: decays


def test_custom_label_depth_and_half_life(kb, pool):
    deep = _kb_with(pool, Policy(projects=Projects("Work", max_depth=2,
                                                   dormancy_half_life_days=10.0)))
    doc = _note(deep, "p/w/doc", "Obsidian.Src.Work.iota.docs")
    _age_ingest_events(pool, doc.id, 10)
    _boost_projects_folder(deep, path="Obsidian.Src.Work")
    imp, mods = _imp(deep, doc.id)
    assert abs(imp - 1.5) < 1e-6                       # 10 quiet days at a 10-day half-life
    assert deep.significance_view(deep.get(doc.id))["project_dormant_days"] == 10
