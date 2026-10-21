"""Deterministic node identity and content hashing."""

import hashlib
from uuid import UUID, uuid5

# Fixed namespace for this knowledge base. Do not change — it would re-key every node.
KB_NAMESPACE = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")


def node_id(source: str, external_id: str) -> UUID:
    """Deterministic id for a node, independent of its location in the tree."""
    return uuid5(KB_NAMESPACE, f"{source}:{external_id}")


def content_hash(title: str | None, content: str) -> str:
    """Stable sha256 over a node's searchable text, for change detection."""
    h = hashlib.sha256()
    h.update((title or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(content.encode("utf-8"))
    return h.hexdigest()
