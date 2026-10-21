def test_sync_state_table_exists(pool):
    with pool.connection() as conn:
        row = conn.execute("SELECT to_regclass('public.sync_state') AS t").fetchone()
    assert row["t"] == "sync_state"
