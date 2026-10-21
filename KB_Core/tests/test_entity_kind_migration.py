"""Migration 0034: kind/occurred_at on entities, occurred_at/role on candidates, role on links."""
import psycopg
import pytest


def test_entity_kind_columns_exist(pool):
    with pool.connection() as conn:
        cols: dict[str, set[str]] = {}
        for r in conn.execute(
                """SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_name IN ('entities','entity_candidates','entity_links')""").fetchall():
            cols.setdefault(r["table_name"], set()).add(r["column_name"])
    assert {"kind", "occurred_at"} <= cols["entities"]
    assert {"occurred_at", "role"} <= cols["entity_candidates"]
    assert "role" in cols["entity_links"]


def test_entities_kind_check_constraint(kb, pool):
    with pool.connection() as conn:
        row = conn.execute("SELECT node_id FROM entities LIMIT 1").fetchone()
        if row is None:
            kb.upsert_entity_class("kindcheck seed", active=False)
            row = conn.execute("SELECT node_id FROM entities LIMIT 1").fetchone()
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("UPDATE entities SET kind = 'banana' WHERE node_id = %s",
                              (row["node_id"],))
