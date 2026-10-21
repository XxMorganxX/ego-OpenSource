"""Schema tests for the derived concept layer (migration 0025)."""
from kb_core.domain import NodeRecord


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


def test_concept_tables_exist(kb, pool):
    with pool.connection() as conn:
        for t in ("concept_runs", "concepts", "concept_members"):
            assert conn.execute(
                "SELECT to_regclass(%s) AS r", (f"public.{t}",)).fetchone()["r"] is not None


def test_member_rows_cascade_with_concept_and_node(kb, pool):
    m = kb.upsert(_mem("m1"))
    with pool.connection() as conn:
        run = conn.execute(
            "INSERT INTO concept_runs (params) VALUES ('{}') RETURNING id").fetchone()
        c = conn.execute(
            "INSERT INTO concepts (name, kind, last_run_id) "
            "VALUES ('KB project', 'project', %s) RETURNING id", (run["id"],)).fetchone()
        conn.execute(
            "INSERT INTO concept_members (concept_id, node_id, member_kind, score) "
            "VALUES (%s, %s, 'memory', 0.9)", (c["id"], m.id))
        # node deleted -> member row gone, concept survives (derived layer never blocks deletes)
        conn.execute("DELETE FROM nodes WHERE id = %s", (m.id,))
        left = conn.execute("SELECT count(*) AS n FROM concept_members").fetchone()["n"]
        still = conn.execute("SELECT count(*) AS n FROM concepts").fetchone()["n"]
    assert left == 0 and still == 1


def test_concept_defaults_and_status(kb, pool):
    with pool.connection() as conn:
        c = conn.execute(
            "INSERT INTO concepts (name) VALUES ('x') "
            "RETURNING kind, level, status, member_count").fetchone()
    assert (c["kind"], c["level"], c["status"], c["member_count"]) == ("other", 0, "active", 0)
