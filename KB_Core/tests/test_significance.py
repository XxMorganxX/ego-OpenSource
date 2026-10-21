from kb_core.domain import NodeRecord


def _note(kb, slug, parent_path):
    return kb.upsert(NodeRecord(source="agent", external_id=f"{parent_path}/{slug}",
                                type="note", content=f"{slug} body", slug=slug,
                                parent_path=parent_path))


def test_set_on_file_only_affects_itself(kb):
    a = _note(kb, "a", "projects")
    b = _note(kb, "b", "projects")
    kb.set_importance(a.id, 1.0)
    assert kb.get(a.id).importance == 2.0          # 1.0 base + manual 1.0
    assert kb.get(b.id).importance == 1.0          # sibling untouched


def test_directory_value_cascades_to_descendants(kb):
    child = _note(kb, "x", "projects.kb")
    folder = kb.get_by_path("projects.kb")
    kb.set_importance(folder.id, 1.5)
    assert kb.get(folder.id).importance == 2.5     # folder's own manual boost
    got = kb.get(child.id)
    assert got.importance == 2.5                   # 1.0 base + inherited 1.5
    assert got.importance_modifiers["inherited"] == 1.5


def test_nested_directory_values_stack(kb):
    child = _note(kb, "x", "projects.kb")
    kb.set_importance(kb.get_by_path("projects").id, 0.5)
    kb.set_importance(kb.get_by_path("projects.kb").id, 1.0)
    got = kb.get(child.id)
    assert got.importance_modifiers["inherited"] == 1.5   # 0.5 + 1.0 stacked
    assert got.importance == 2.5                          # 1.0 base + 1.5


def test_unrelated_folder_does_not_leak(kb):
    here = _note(kb, "x", "projects.kb")
    there = _note(kb, "y", "other")
    kb.set_importance(kb.get_by_path("projects").id, 1.0)
    assert kb.get(there.id).importance == 1.0             # different subtree, no boost
    assert kb.get(here.id).importance == 2.0


def test_source_recompute_applies_inherited(kb):
    # Simulates the post-ingest RecomputeImportance stage (recompute_importance(source=...)):
    # a new file dropped under a boosted folder picks up the cascade.
    _note(kb, "seed", "projects")                # auto-creates the projects folder
    kb.set_importance(kb.get_by_path("projects").id, 1.0)
    child = _note(kb, "fresh", "projects")       # upsert does NOT recompute
    assert kb.get(child.id).importance == 1.0    # not yet
    kb.recompute_importance(source="agent")
    assert kb.get(child.id).importance == 2.0    # inherited applied by the source recompute


def test_move_into_boosted_folder_raises_inherited(kb):
    _note(kb, "seed", "projects")                      # ensure the projects folder exists
    loose = _note(kb, "x", "inbox")
    kb.set_importance(kb.get_by_path("projects").id, 1.0)
    assert kb.get(loose.id).importance == 1.0          # not under projects yet
    kb.move(loose.id, "projects")
    moved = kb.get_by_path("projects.x")
    assert moved.importance == 2.0                      # now inherits projects' 1.0
    assert moved.importance_modifiers["inherited"] == 1.0


def test_move_out_of_boosted_folder_drops_inherited(kb):
    inside = _note(kb, "x", "projects")
    kb.set_importance(kb.get_by_path("projects").id, 1.0)
    assert kb.get(inside.id).importance == 2.0
    kb.move(inside.id, "inbox")
    moved = kb.get_by_path("inbox.x")
    assert moved.importance == 1.0                      # boost gone
    assert moved.importance_modifiers["inherited"] == 0.0


def test_significance_view_breakdown(kb):
    child = _note(kb, "x", "projects.kb")
    kb.set_importance(kb.get_by_path("projects").id, 0.5)
    kb.set_importance(kb.get_by_path("projects.kb").id, 1.0)
    view = kb.significance_view(kb.get(child.id))

    by_name = {f["name"]: f for f in view["factors"]}
    assert by_name["inherited"]["value"] == 1.5
    assert by_name["inherited"]["coefficient"] == 1.0
    assert by_name["inherited"]["contribution"] == 1.5
    assert by_name["manual"]["value"] == 0.0

    assert [s["path"] for s in view["inherited_from"]] == ["projects", "projects.kb"]
    assert view["inherited_from"][0]["value"] == 0.5
    assert view["descendant_count"] == 0            # a leaf note
    # fresh node (age ~0): the boost isn't decayed yet, so effective == stored importance
    assert view["effective_importance"] <= kb.get(child.id).importance
    assert view["age_days"] >= 0


def test_significance_view_folder_descendant_count(kb):
    _note(kb, "x", "projects.kb")
    _note(kb, "y", "projects.kb")
    folder = kb.get_by_path("projects.kb")
    view = kb.significance_view(folder)
    assert view["descendant_count"] == 2            # the two notes under it
