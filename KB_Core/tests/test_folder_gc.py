from kb_core.domain import NodeRecord
from kb_core.entities import ENTITY_ROOT


def _note(**over):
    base = dict(source="obsidian", external_id="n1", type="note", content="c",
                slug="note", parent_path="")
    base.update(over)
    return NodeRecord(**base)


def _paths(kb):
    return {n.path for n in kb.list(limit=10000)}


def test_empty_folder_subtree_is_pruned(kb):
    n = kb.upsert(_note(external_id="n1", slug="note", parent_path="a.b.c"))
    assert {"a", "a.b", "a.b.c"} <= _paths(kb)         # mkdir -p made the chain
    kb.delete(n.id)                                    # note gone -> chain is now empty
    assert kb.prune_empty_folders() == 3               # whole dead subtree a / a.b / a.b.c
    assert not ({"a", "a.b", "a.b.c"} & _paths(kb))


def test_folder_holding_a_note_is_kept(kb):
    kb.upsert(_note(external_id="k", slug="keep", parent_path="proj"))
    dead = kb.upsert(_note(external_id="d", slug="dead", parent_path="proj.old"))
    kb.delete(dead.id)
    assert kb.prune_empty_folders() == 1               # only the emptied 'proj.old'
    paths = _paths(kb)
    assert {"proj", "proj.keep"} <= paths              # 'proj' still holds a note -> kept
    assert "proj.old" not in paths


def test_prune_empty_folders_logs_node_deleted(kb):
    n = kb.upsert(_note(external_id="x", slug="note", parent_path="ghost"))
    kb.delete(n.id)
    kb.prune_empty_folders()
    deleted = [r for r in kb.ingest_log() if r["event"] == "node_deleted"]
    assert any(r["path"] == "ghost" for r in deleted)  # the folder removal is observable


def test_entity_root_is_preserved_when_empty(kb):
    cls = kb.upsert_entity_class("people", active=True)
    kb.delete(cls.node.id, recursive=True)             # remove the only entity
    assert ENTITY_ROOT in _paths(kb)                   # reserved root now empty…
    kb.prune_empty_folders()
    assert ENTITY_ROOT in _paths(kb)                   # …but intentionally preserved
