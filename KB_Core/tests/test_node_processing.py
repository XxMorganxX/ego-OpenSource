from kb_core.domain import NodeRecord


def _rec(**over):
    base = dict(source="agent", external_id="m1", type="note",
                content="hello world", slug="hello", parent_path="")
    base.update(over)
    return NodeRecord(**base)


def _proc_rows(pool, node_id, task=None):
    sql = ("SELECT task, status, started_at, last_done_at, run_count "
           "FROM node_processing WHERE node_id = %s")
    params = [node_id]
    if task is not None:
        sql += " AND task = %s"
        params.append(task)
    sql += " ORDER BY task"
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


def test_node_processing_table_exists(pool):
    with pool.connection() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'node_processing'"
        ).fetchall()}
    assert {"node_id", "task", "status", "queued_at", "started_at",
            "last_done_at", "run_count", "last_error", "attempts", "updated_at"} <= cols


def test_enrichment_status_projects_into_node_processing(kb, pool):
    n = kb.upsert(_rec())
    assert _proc_rows(pool, n.id, "enrichment") == []          # 'none' -> no row yet
    kb.flag_stale_enrichment()                                 # none -> pending
    row = _proc_rows(pool, n.id, "enrichment")[0]
    assert row["status"] == "pending" and row["run_count"] == 0
    kb.claim_enrichment()                                      # pending -> processing
    row = _proc_rows(pool, n.id, "enrichment")[0]
    assert row["status"] == "processing" and row["started_at"] is not None
    kb.mark_enriched(n.id)                                     # processing -> done
    row = _proc_rows(pool, n.id, "enrichment")[0]
    assert row["status"] == "done"
    assert row["run_count"] == 1 and row["last_done_at"] is not None


def test_done_reasserted_does_not_double_count(kb, pool):
    n = kb.upsert(_rec())
    kb.flag_stale_enrichment()
    kb.claim_enrichment()
    kb.mark_enriched(n.id)
    kb.mark_enriched(n.id)                                     # re-assert 'done' (no transition)
    assert _proc_rows(pool, n.id, "enrichment")[0]["run_count"] == 1


def test_run_count_increments_per_done_cycle(kb, pool):
    n = kb.upsert(_rec())
    kb.flag_stale_enrichment()
    kb.claim_enrichment()
    kb.mark_enriched(n.id)
    with pool.connection() as conn:                           # mimic a raw re-queue site
        conn.execute("UPDATE nodes SET enrichment_status='pending', "
                     "enriched_hash=NULL WHERE id=%s", (n.id,))
    kb.claim_enrichment()
    kb.mark_enriched(n.id)
    assert _proc_rows(pool, n.id, "enrichment")[0]["run_count"] == 2


def test_backfill_seeds_existing_enriched_nodes(kb, pool):
    # The migration's backfill INSERT...SELECT seeds rows for nodes that predate the table.
    # Reproduce it: project a 'done' node, delete the row (as if the table never existed),
    # then run the exact backfill statement and assert the seed row is correct.
    n = kb.upsert(_rec())
    kb.flag_stale_enrichment()
    kb.claim_enrichment()
    kb.mark_enriched(n.id)
    with pool.connection() as conn:
        conn.execute("DELETE FROM node_processing WHERE node_id=%s", (n.id,))
        conn.execute(
            "INSERT INTO node_processing "
            "  (node_id, task, status, queued_at, last_done_at, run_count, updated_at) "
            "SELECT id, 'enrichment', enrichment_status, ingested_at, enriched_at, "
            "       CASE WHEN enriched_at IS NOT NULL THEN 1 ELSE 0 END, now() "
            "FROM nodes WHERE enrichment_status <> 'none' "
            "ON CONFLICT (node_id, task) DO NOTHING"
        )
    row = _proc_rows(pool, n.id, "enrichment")[0]
    assert row["status"] == "done"
    assert row["run_count"] == 1 and row["last_done_at"] is not None


def test_request_reanalysis_queues_recrawl_row(kb, pool):
    n = kb.upsert(_rec())
    kb.request_reanalysis(n.id)
    row = _proc_rows(pool, n.id, "recrawl")[0]
    assert row["status"] == "pending"


def test_mark_analyzed_marks_recrawl_done(kb, pool):
    n = kb.upsert(_rec())
    kb.request_reanalysis(n.id)                # recrawl pending
    kb.mark_analyzed(n.id)                      # -> done
    row = _proc_rows(pool, n.id, "recrawl")[0]
    assert row["status"] == "done"
    assert row["last_done_at"] is not None and row["run_count"] == 1


def test_mark_analyzed_does_not_create_enrichment_recrawl_noise(kb, pool):
    # mark_analyzed alone (without a prior request_reanalysis) must NOT spawn a recrawl row.
    n = kb.upsert(_rec())
    kb.flag_stale_enrichment()
    kb.claim_enrichment()
    kb.mark_enriched(n.id)
    assert _proc_rows(pool, n.id, "recrawl") == []


def test_processing_map_returns_only_active_tasks(kb, pool):
    n = kb.upsert(_rec())
    kb.flag_stale_enrichment()                 # enrichment pending
    kb.request_reanalysis(n.id)                # recrawl pending
    m = kb.processing_map([n.id])
    assert {d["task"] for d in m[n.id]} == {"enrichment", "recrawl"}
    kb.mark_analyzed(n.id)                      # recrawl -> done, drops out of the map
    m2 = kb.processing_map([n.id])
    assert {d["task"] for d in m2[n.id]} == {"enrichment"}


def test_processing_map_empty_for_clean_node(kb, pool):
    n = kb.upsert(_rec())
    assert kb.processing_map([n.id]) == {}
    assert kb.processing_map([]) == {}
