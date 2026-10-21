"""doc_kind facet: column plumbing + KB reads/filters."""
from kb_core.domain import NodePatch, NodeRecord


def _note(kb, ext, slug, **over):
    base = dict(source="agent", external_id=ext, type="note",
                content=f"body {slug}", slug=slug, parent_path="notes")
    base.update(over)
    return kb.upsert(NodeRecord(**base))


def test_doc_kind_defaults_null_and_roundtrips(kb):
    node = _note(kb, "dk1", "dk1")
    assert node.doc_kind is None
    updated = kb.update(node.id, NodePatch(doc_kind="meeting notes"))
    assert updated.doc_kind == "meeting notes"
    assert kb.get(node.id).doc_kind == "meeting notes"


def test_update_without_doc_kind_preserves_it(kb):
    node = _note(kb, "dk2", "dk2")
    kb.update(node.id, NodePatch(doc_kind="meeting notes"))
    after = kb.update(node.id, NodePatch(title="Renamed"))
    assert after.doc_kind == "meeting notes"


def _kinded(kb, n, kind, prefix):
    for i in range(n):
        node = _note(kb, f"{prefix}{i}", f"{prefix}{i}",
                     content=f"standup sync discussion {prefix}{i}")
        kb.update(node.id, NodePatch(doc_kind=kind))


def test_doc_kinds_for_prompt_is_support_ranked(kb):
    _kinded(kb, 3, "meeting notes", "pm")
    _kinded(kb, 1, "journal entry", "pj")
    assert kb.doc_kinds_for_prompt() == ["meeting notes", "journal entry"]


def test_doc_kind_rollup_gates_by_support(kb):
    _kinded(kb, 3, "meeting notes", "rm")
    _kinded(kb, 1, "implementation plan", "rp")
    assert kb.doc_kind_rollup() == [
        {"kind": "meeting notes", "count": 3, "established": True}]
    everything = kb.doc_kind_rollup(include_all=True)
    assert {(r["kind"], r["established"]) for r in everything} == {
        ("meeting notes", True), ("implementation plan", False)}


def test_dead_memories_do_not_count(kb):
    _kinded(kb, 3, "meeting notes", "dm")
    victim = kb.list(under="notes", kind="meeting notes")[0]
    kb.mark_dead(victim.id, reason="stale")
    assert kb.doc_kind_rollup(include_all=True)[0]["count"] == 2


def test_list_and_search_filter_by_kind(kb):
    _kinded(kb, 2, "meeting notes", "fm")
    _kinded(kb, 1, "implementation plan", "fp")
    listed = kb.list(under="notes", kind="meeting notes")
    assert len(listed) == 2
    assert all(n.doc_kind == "meeting notes" for n in listed)
    hits = kb.search("standup", kind="implementation plan")
    assert len(hits) == 1 and hits[0].node.doc_kind == "implementation plan"
    assert len(kb.search("standup")) == 3          # no filter -> everything
