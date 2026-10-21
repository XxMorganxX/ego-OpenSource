def test_inbox_table_exists(pool):
    with pool.connection() as conn:
        t = conn.execute("SELECT to_regclass('public.ingest_inbox') AS t").fetchone()["t"]
    assert t == "ingest_inbox"


def test_nodes_enrichment_columns_exist(pool):
    with pool.connection() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'nodes'"
        ).fetchall()}
    assert {"enrichment_status", "enriched_at", "enriched_hash"} <= cols
