"""Tests for reembed_dirty_entities — batched, hash-guarded embedding pass.

For each dirty entity, assembles embed text via build_embed_text, skips
the model call when text is unchanged (hash guard), stores the vector when it changed.
"""




class StubEmbed:
    """Deterministic embed: vector keyed off text length, padded to 1024."""
    def __init__(self): self.calls = 0
    def complete(self, *a, **k): raise AssertionError("not used")
    def embed(self, texts):
        self.calls += 1
        return [[float(len(t))] + [0.0] * 1023 for t in texts]


def test_reembed_embeds_dirty_and_clears_flag(kb):
    e = kb.upsert_entity("Avery")
    client = StubEmbed()
    n = kb.reembed_dirty_entities(client, batch=32)
    assert n >= 1
    assert kb.is_embed_dirty(e.node.id) is False
    assert kb.get_embedding(e.node.id) is not None


def test_reembed_hash_guard_skips_model_call(kb):
    e = kb.upsert_entity("Avery")
    client = StubEmbed()
    kb.reembed_dirty_entities(client, batch=32)        # first embed
    first_calls = client.calls
    # re-dirty with no actual text change
    with kb._pool.connection() as conn:
        conn.execute("UPDATE entities SET embed_dirty = true WHERE node_id = %s", (e.node.id,))
    kb.reembed_dirty_entities(client, batch=32)
    assert client.calls == first_calls                 # text unchanged -> no new embed call
    assert kb.is_embed_dirty(e.node.id) is False       # but flag cleared
