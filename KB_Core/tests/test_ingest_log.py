from kb_core.domain import NodeRecord


def _rec(**over):
    base = dict(source="agent", external_id="m1", type="note",
                content="hello world", slug="hello", parent_path="")
    base.update(over)
    return NodeRecord(**base)


def test_upsert_logs_node_created_then_updated(kb):
    node = kb.upsert(_rec(external_id="L1", slug="l1", content="one"))
    rows = kb.ingest_log()
    assert rows[0]["event"] == "node_created"
    assert rows[0]["source"] == "agent"
    assert rows[0]["external_id"] == "L1"
    assert rows[0]["node_id"] == node.id
    assert rows[0]["path"] == "l1"

    kb.upsert(_rec(external_id="L1", slug="l1", content="two"))   # same id -> update
    assert kb.ingest_log()[0]["event"] == "node_updated"


def test_mark_enriched_logs_enrichment_done(kb):
    node = kb.upsert(_rec(external_id="L2", slug="l2", content="x"))
    kb.mark_enriched(node.id)
    done = [r for r in kb.ingest_log() if r["event"] == "enrichment_done"]
    assert len(done) == 1
    assert done[0]["node_id"] == node.id
    assert done[0]["content_hash"] == kb.get(node.id).content_hash


def test_ingest_log_orders_newest_first_and_limits(kb):
    for i in range(3):
        kb.upsert(_rec(external_id=f"n{i}", slug=f"n{i}"))
    rows = kb.ingest_log(limit=2)
    assert len(rows) == 2
    assert rows[0]["at"] >= rows[1]["at"]


def test_ingest_log_filters_by_source(kb):
    kb.upsert(_rec(source="agent", external_id="a1", slug="a1"))
    kb.upsert(_rec(source="webhook", external_id="w1", slug="w1"))
    assert {r["source"] for r in kb.ingest_log(source="webhook")} == {"webhook"}


def test_ingest_log_filters_by_event(kb):
    n = kb.upsert(_rec(external_id="E1", slug="e1", content="x"))
    kb.mark_enrichment_failed(n.id, error="ValueError: boom")
    failed = kb.ingest_log(event="enrichment_failed")
    assert [r["event"] for r in failed] == ["enrichment_failed"]
    assert failed[0]["node_id"] == n.id
    assert failed[0]["detail"]["error"] == "ValueError: boom"


def test_unchanged_resync_is_not_logged(kb):
    rec = _rec(external_id="L3", slug="l3", content="same")
    kb.upsert(rec)
    kb.upsert(rec)   # identical content_hash -> idempotent re-sync, no new log row
    kb.upsert(rec)
    events = [r["event"] for r in kb.ingest_log(source="agent")]
    assert events == ["node_created"]   # exactly one creation, no node_updated noise


def test_delete_logs_node_deleted(kb):
    n = kb.upsert(_rec(external_id="D1", slug="d1"))
    assert kb.delete(n.id) == 1
    deleted = [r for r in kb.ingest_log() if r["event"] == "node_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["node_id"] == n.id and deleted[0]["external_id"] == "D1"


def test_prune_logs_node_deleted_per_node(kb):
    kb.upsert(_rec(source="obsidian", external_id="a", slug="a"))
    kb.upsert(_rec(source="obsidian", external_id="b", slug="b"))
    assert kb.prune("obsidian", {"a"}) == 1          # 'b' is no longer present -> pruned
    deleted = [r for r in kb.ingest_log(source="obsidian") if r["event"] == "node_deleted"]
    assert [d["external_id"] for d in deleted] == ["b"]
