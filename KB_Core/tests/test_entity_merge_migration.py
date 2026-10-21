def test_entity_merge_schema_exists(pool):
    with pool.connection() as conn:
        cols = {r["column_name"]: r["data_type"] for r in conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'entities'").fetchall()}
        assert "embedding" in cols and cols["embedding"] == "USER-DEFINED"  # pgvector type
        assert cols["embed_dirty"] == "boolean"
        assert "embed_text_hash" in cols

        tables = {r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'").fetchall()}
        assert "entity_merge_candidates" in tables
        assert "entity_merge_blocks" in tables

def test_candidates_pair_is_unique(pool):
    # the UNIQUE(left_id, right_id) constraint exists
    with pool.connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM pg_constraint "
            "WHERE conrelid = 'entity_merge_candidates'::regclass AND contype = 'u'"
        ).fetchone()["n"]
        assert n >= 1
