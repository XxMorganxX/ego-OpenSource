import pytest
from kb_core.paths import (
    join_path, parent_of, labels, ancestor_paths, leaf, sanitize_slug,
)


def test_join_path_with_parent():
    assert join_path("projects.kb_mcp", "notes") == "projects.kb_mcp.notes"


def test_join_path_root():
    assert join_path("", "projects") == "projects"


def test_parent_of():
    assert parent_of("projects.kb_mcp.notes") == "projects.kb_mcp"
    assert parent_of("projects") == ""


def test_labels():
    assert labels("a.b.c") == ["a", "b", "c"]
    assert labels("") == []


def test_leaf():
    assert leaf("a.b.c") == "c"
    assert leaf("a") == "a"


def test_ancestor_paths():
    assert ancestor_paths("a.b.c") == ["a", "a.b"]
    assert ancestor_paths("a") == []
    assert ancestor_paths("") == []


def test_sanitize_slug_replaces_bad_chars():
    assert sanitize_slug("Vendor Quote #3!") == "Vendor_Quote_3"


def test_sanitize_slug_raises_on_empty():
    with pytest.raises(ValueError):
        sanitize_slug("!!!")
