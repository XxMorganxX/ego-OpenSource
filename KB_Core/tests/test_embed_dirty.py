"""Tests for embed_dirty flag tracking on entity mutation paths.

Dirty tracking — every mutation that changes what an entity's embedding
should represent (name/aliases/description or mention context) sets embed_dirty=true
so a later background pass knows to re-embed only what changed.
"""


from kb_core.domain import NodeRecord


def _mem(eid: str, content: str = "c") -> NodeRecord:
    return NodeRecord(source="agent", external_id=eid, type="note", content=content, slug=eid)


def test_new_entity_is_dirty(kb):
    """A freshly created entity starts embed_dirty=true (migration default)."""
    e = kb.upsert_entity("Avery")
    assert kb.is_embed_dirty(e.node.id) is True


def test_clearing_then_linking_redirties(kb):
    """After clearing the flag, adding a memory->entity mention link re-dirties it."""
    e = kb.upsert_entity("Avery")
    kb.clear_embed_dirty(e.node.id, vector=[0.0] * 1024, text_hash="h")
    assert kb.is_embed_dirty(e.node.id) is False

    m = kb.upsert(_mem("n1", "hi avery"))
    kb.link_entity(m.id, e.node.id, "mentions")
    assert kb.is_embed_dirty(e.node.id) is True


def test_unlink_redirties(kb):
    """After clearing the flag, retracting a memory's influence (which removes its
    entity_links) re-dirties the affected entity.

    There is no `kb.unlink_entity(...)`. The real
    path that removes a memory's mention links is `set_entity_eligible(memory_id, False)`,
    which internally calls `_retract_influence` — deleting that memory's entity_links rows.
    We use that path here to test the intent: removing a mention re-dirties the entity.

    We need two memories mentioning Avery so the entity is NOT orphaned (and thus NOT
    deleted by reconcile_instances) when one memory's influence is retracted.
    """
    e = kb.upsert_entity("Avery")
    m1 = kb.upsert(_mem("n1", "hi avery"))
    m2 = kb.upsert(_mem("n2", "bye avery"))  # anchor — keeps entity alive after retract
    kb.link_entity(m1.id, e.node.id, "mentions")
    kb.link_entity(m2.id, e.node.id, "mentions")
    kb.clear_embed_dirty(e.node.id, vector=[0.0] * 1024, text_hash="h")
    assert kb.is_embed_dirty(e.node.id) is False

    # Retract m1's entity influence (removes its entity_links row for Avery).
    # m2 still mentions Avery, so the entity survives reconcile_instances.
    kb.set_entity_eligible(m1.id, False, reason="test")
    assert kb.is_embed_dirty(e.node.id) is True


def test_upsert_entity_update_redirties(kb):
    """Updating an entity via upsert with a description change re-dirties it."""
    e = kb.upsert_entity("Alice")
    kb.clear_embed_dirty(e.node.id, vector=[0.0] * 1024, text_hash="h")
    assert kb.is_embed_dirty(e.node.id) is False

    # Re-upsert with a description change (triggers ON CONFLICT DO UPDATE).
    kb.upsert_entity("Alice", description="A test person")
    assert kb.is_embed_dirty(e.node.id) is True


def test_upsert_entity_class_update_redirties(kb):
    """Updating an entity class via upsert re-dirties it."""
    ec = kb.upsert_entity_class("People")
    kb.clear_embed_dirty(ec.node.id, vector=[0.0] * 1024, text_hash="h")
    assert kb.is_embed_dirty(ec.node.id) is False

    # Re-upsert the class with a description — triggers ON CONFLICT DO UPDATE.
    kb.upsert_entity_class("People", description="A category")
    assert kb.is_embed_dirty(ec.node.id) is True


def test_refresh_entity_contribution_redirties(kb):
    """refresh_entity_contribution (classes=True) removes belongs_to links and
    should dirty the affected entity class."""
    ec = kb.upsert_entity_class("People", active=True)
    m = kb.upsert(_mem("n1", "people note"))
    kb.link_entity(m.id, ec.node.id, "belongs_to")
    kb.clear_embed_dirty(ec.node.id, vector=[0.0] * 1024, text_hash="h")
    assert kb.is_embed_dirty(ec.node.id) is False

    kb.refresh_entity_contribution(m.id, classes=True, instances=False)
    assert kb.is_embed_dirty(ec.node.id) is True
