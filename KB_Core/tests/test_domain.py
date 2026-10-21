from kb_core.domain import NodeRecord, NodePatch


def test_node_record_defaults():
    rec = NodeRecord(source="agent", external_id="x", type="note",
                     content="hello", slug="hello")
    assert rec.parent_path == ""
    assert rec.tags == []
    assert rec.title is None
    assert rec.extension is None


def test_node_patch_defaults_are_none():
    patch = NodePatch()
    assert patch.title is None
    assert patch.content is None
    assert patch.tags is None
