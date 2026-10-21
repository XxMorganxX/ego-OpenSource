from uuid import UUID
from kb_core.identity import node_id, content_hash


def test_node_id_is_deterministic():
    a = node_id("agent", "abc")
    b = node_id("agent", "abc")
    assert a == b
    assert isinstance(a, UUID)


def test_node_id_differs_by_source_and_external_id():
    assert node_id("agent", "abc") != node_id("gmail", "abc")
    assert node_id("agent", "abc") != node_id("agent", "xyz")


def test_content_hash_is_stable_and_sensitive():
    h1 = content_hash("Title", "body")
    assert h1 == content_hash("Title", "body")
    assert h1 != content_hash("Title", "different")
    assert h1 != content_hash(None, "body")
