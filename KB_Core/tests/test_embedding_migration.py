"""0021 adds the embedding vector + its text hash, and the hash round-trips into Node."""

from kb_core.domain import NodeRecord


def test_embedding_columns_exist(pool):
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT column_name, data_type, udt_name
               FROM information_schema.columns
               WHERE table_name = 'nodes'
                 AND column_name IN ('embedding', 'embedded_hash')
               ORDER BY column_name"""
        ).fetchall()
    cols = {r["column_name"]: r for r in rows}
    assert set(cols) == {"embedded_hash", "embedding"}
    assert cols["embedded_hash"]["data_type"] == "text"
    assert cols["embedding"]["udt_name"] == "vector"


def test_new_node_has_null_embedded_hash(kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="e1", type="note",
                                content="hello world", slug="hello", title="T"))
    assert kb.get(node.id).embedded_hash is None
