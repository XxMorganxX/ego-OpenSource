import pytest


@pytest.mark.parametrize("table", ["edges", "entities", "entity_candidates"])
def test_table_exists(pool, table):
    with pool.connection() as conn:
        t = conn.execute(
            "SELECT to_regclass(%s) AS t", (f"public.{table}",)
        ).fetchone()["t"]
    assert t == table


def test_edges_columns(pool):
    with pool.connection() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'edges'"
        ).fetchall()}
    assert {"source_id", "target_id", "relation_type", "weight"} <= cols


def test_entities_columns(pool):
    with pool.connection() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'entities'"
        ).fetchall()}
    assert {"node_id", "entity_type", "canonical_name", "aliases", "description"} <= cols
