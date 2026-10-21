"""Tests for entity_class_links.origin column, UNTYPED_CATEGORY constant,
and origin-aware _link_instance_class (human-precedence semantics)."""

from kb_core.entities import (
    ENTITY_CLASS,
    ENTITY_SOURCE,
    UNTYPED_CATEGORY,
    instance_external_id,
    entity_external_id,
)
from kb_core.identity import node_id
from kb_core.knowledge_base import KnowledgeBase


# ── helpers ──────────────────────────────────────────────────────────────────

def _inst_id(name: str):
    return node_id(ENTITY_SOURCE, instance_external_id(name))


def _class_id(name: str):
    return node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, name))


def _link_origin(kb: KnowledgeBase, inst_id, cls_id) -> str:
    """Read the current origin for a given (instance_id, class_id) link."""
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT origin FROM entity_class_links WHERE instance_id = %s AND class_id = %s",
            (inst_id, cls_id),
        ).fetchone()
    assert row is not None, "link row not found"
    return row["origin"]


# ── (a) schema: origin column exists with correct CHECK ──────────────────────

def test_origin_column_exists_with_check(pool):
    """entity_class_links.origin must exist and have a check constraint."""
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT column_name, column_default, is_nullable
               FROM information_schema.columns
               WHERE table_name = 'entity_class_links' AND column_name = 'origin'""",
        ).fetchone()
    assert row is not None, "origin column missing from entity_class_links"
    assert row["column_default"] is not None and "derived" in row["column_default"]
    assert row["is_nullable"] == "NO"


def test_origin_check_constraint_rejects_bad_value(pool):
    """The CHECK (origin IN ('derived','human')) must reject other values."""
    import pytest

    with pool.connection() as conn:
        # We need a real instance/class row to attempt an insert
        conn.execute("SAVEPOINT sp1")
        try:
            conn.execute(
                """INSERT INTO entity_class_links (instance_id, class_id, weight, origin)
                   VALUES (gen_random_uuid(), gen_random_uuid(), NULL, 'invalid')"""
            )
            conn.execute("RELEASE SAVEPOINT sp1")
            pytest.fail("Expected CHECK constraint violation")
        except Exception as e:
            conn.execute("ROLLBACK TO SAVEPOINT sp1")
            assert "origin" in str(e).lower() or "check" in str(e).lower() or "invalid" in str(e).lower()


# ── (b) UNTYPED_CATEGORY constant ────────────────────────────────────────────

def test_untyped_category_importable():
    """UNTYPED_CATEGORY must be importable and equal '__untyped__'."""
    assert UNTYPED_CATEGORY == "__untyped__"


# ── (c) human-precedence in _link_instance_class ─────────────────────────────

def test_link_instance_class_human_precedence(kb):
    """Human-precedence: derived → human upgrade works; human → derived does NOT downgrade."""
    # Set up a class and instance in the DB via promote_entity (creates derived link)
    kb.upsert_entity_class("organization", active=True)
    inst_node = kb.promote_entity("organization", "Acme Corp")

    inst_id = inst_node.node.id
    cls_id = _class_id("organization")

    # Verify the link was created (promotion creates it with default 'derived')
    origin = _link_origin(kb, inst_id, cls_id)
    assert origin == "derived", f"Expected 'derived' after promotion, got {origin!r}"

    # Upgrade to human
    with kb._pool.connection() as conn:
        KnowledgeBase._link_instance_class(conn, inst_id, cls_id, origin="human")
    origin = _link_origin(kb, inst_id, cls_id)
    assert origin == "human", f"Expected 'human' after explicit human link, got {origin!r}"

    # Attempt to downgrade back to derived — must NOT succeed
    with kb._pool.connection() as conn:
        KnowledgeBase._link_instance_class(conn, inst_id, cls_id, origin="derived")
    origin = _link_origin(kb, inst_id, cls_id)
    assert origin == "human", "Human link downgraded to 'derived' — precedence rule broken!"
