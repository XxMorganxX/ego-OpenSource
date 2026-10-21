"""DB probe feeding the churn-triggered consolidate daemon.

last_change deliberately tracks nodes.enriched_at, NOT nodes.updated_at: an always-due
source (period_seconds=0) re-upserts unchanged notes every ~20s scheduler tick, which
would hold the 180s quiet window shut forever and starve the churn path."""
from kb_core.domain import NodeRecord
from kb_core.entities import BELONGS_TO


def test_empty_db_signal(kb):
    sig = kb.consolidation_signal()
    assert sig["last_change"] is None
    assert sig["backlog"] == 0
    assert sig["last_run_finished"] is None
    assert sig["db_now"].tzinfo is not None            # DB clock, tz-aware


def test_backlog_counts_pending_and_processing_not_failed(kb, pool):
    statuses = ["pending", "processing", "failed", "done"]
    for i, status in enumerate(statuses):
        kb.upsert(NodeRecord(source="agent", external_id=f"m{i}", type="note",
                             content="hello", slug=f"m{i}"))
    with pool.connection() as c:
        for i, status in enumerate(statuses):
            c.execute("UPDATE nodes SET enrichment_status = %s WHERE external_id = %s",
                      (status, f"m{i}"))
    assert kb.consolidation_signal()["backlog"] == 2   # failed/done excluded


def test_last_change_takes_the_newest_writer(kb, pool):
    note = kb.upsert(NodeRecord(source="agent", external_id="m1", type="note",
                                content="hello", slug="m1"))
    cls = kb.upsert_entity_class("projects", active=True)
    kb.link_entity(note.id, cls.node.id, BELONGS_TO)
    with pool.connection() as c:                       # future-date the link write
        c.execute("UPDATE entity_links SET updated_at = now() + interval '1 hour'")
    sig = kb.consolidation_signal()
    assert sig["last_change"] is not None
    assert sig["last_change"] > sig["db_now"]          # GREATEST picked entity_links


def test_sync_sweep_updated_at_bump_does_not_move_last_change(kb, pool):
    """A re-sync of unchanged content bumps nodes.updated_at (upsert is unconditional
    there) — the probe must NOT read that as churn, or an always-due source starves
    the quiet window permanently."""
    kb.upsert(NodeRecord(source="agent", external_id="m1", type="note",
                         content="hello", slug="m1"))
    with pool.connection() as c:
        c.execute("UPDATE nodes SET updated_at = now()")
    assert kb.consolidation_signal()["last_change"] is None


def test_enrichment_completion_moves_last_change(kb, pool):
    kb.upsert(NodeRecord(source="agent", external_id="m1", type="note",
                         content="hello", slug="m1"))
    with pool.connection() as c:                       # what the enricher's done-path sets
        c.execute("UPDATE nodes SET enrichment_status = 'done', enriched_at = now() "
                  "WHERE type = 'note'")
    assert kb.consolidation_signal()["last_change"] is not None


def test_last_run_finished_ignores_failed_runs(kb):
    failed = kb.begin_concept_run({})
    kb.finish_concept_run(failed, status="failed", stats={})
    assert kb.consolidation_signal()["last_run_finished"] is None
    done = kb.begin_concept_run({})
    kb.finish_concept_run(done, status="done", stats={})
    assert kb.consolidation_signal()["last_run_finished"] is not None
