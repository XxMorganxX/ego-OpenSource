def test_importance_columns_exist(pool):
    with pool.connection() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'nodes'"
        ).fetchall()}
    assert {"importance", "importance_modifiers", "access_count", "last_accessed_at"} <= cols
