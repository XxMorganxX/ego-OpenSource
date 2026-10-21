"""set_embedding writes the vector (as a ::vector cast) and the text hash together."""

from kb_core.domain import NodeRecord


def test_set_embedding_round_trips(kb, pool):
    node = kb.upsert(NodeRecord(source="agent", external_id="se1", type="note",
                                content="hello world", slug="se1", title="T"))
    kb.set_embedding(node.id, [0.1] * 1024, embedded_hash="abc123")

    assert kb.get(node.id).embedded_hash == "abc123"
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT embedding IS NOT NULL AS has_vec, vector_dims(embedding) AS dims "
            "FROM nodes WHERE id = %s", (node.id,)
        ).fetchone()
    assert row["has_vec"] is True
    assert row["dims"] == 1024
