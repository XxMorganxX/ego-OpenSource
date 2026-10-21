from kb_core.adapters.webhook.decoder import WebhookDecoder
from kb_core.domain import NodeRecord
from kb_core.identity import node_id
from kb_core.ingest.drain import InboxDrainService
from kb_core.ingest.inbox import Inbox


def _rec_webhook(eid):
    return NodeRecord(source="webhook", external_id=eid, type="note",
                      content="x", slug=eid)


def test_drain_upserts_and_marks_processed(kb, pool):
    Inbox(pool).append("webhook", external_id="a", event_type="upsert",
                        payload={"external_id": "a", "title": "Note A", "content": "body"})
    res = InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert res.upserted == 1
    assert kb.get_by_path("Note_A") is not None
    assert Inbox(pool).claim_pending("webhook") == []          # event consumed
    assert kb.get_by_path("Note_A").enrichment_status == "pending"   # post-stage ran


def test_drain_handles_delete_events(kb, pool):
    box = Inbox(pool)
    box.append("webhook", external_id="d", event_type="upsert",
               payload={"external_id": "d", "title": "Doomed", "content": "x"})
    InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    box.append("webhook", external_id="d", event_type="delete", payload={"external_id": "d"})
    res = InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert res.deleted == 1
    assert kb.get_by_path("Doomed") is None


def test_drain_never_prunes_unseen(kb, pool):
    kb.upsert(_rec_webhook("keep"))                            # a pre-existing webhook node
    Inbox(pool).append("webhook", external_id="new", event_type="upsert",
                       payload={"external_id": "new", "title": "New", "content": "y"})
    InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert kb.external_ids_for_source("webhook") == {"keep", "new"}   # 'keep' survives


def test_drain_bad_event_is_marked_failed_and_continues(kb, pool):
    box = Inbox(pool)
    bad = box.append("webhook", external_id=None, event_type="delete", payload={})  # no id
    box.append("webhook", external_id="ok", event_type="upsert",
               payload={"external_id": "ok", "title": "OK", "content": "z"})
    res = InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert res.upserted == 1 and len(res.errors) == 1
    assert kb.get_by_path("OK") is not None
    assert box.claim_pending("webhook") == []                 # bad one marked failed, not pending


# --- stuck records: a failed inbox event is visible on the dashboard, not only in the row ---

def test_drain_failure_logs_ingest_failed_keyed_by_the_inbox_external_id(kb, pool):
    # '!!!' sanitizes to an empty slug, so the decoder refuses the payload.
    Inbox(pool).append("webhook", external_id="bad", event_type="upsert",
                       payload={"external_id": "bad", "slug": "!!!", "content": "x"})
    res = InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert len(res.errors) == 1                              # daemon line unchanged
    rows = kb.ingest_log(event="ingest_failed")
    assert len(rows) == 1
    assert rows[0]["source"] == "webhook"
    assert rows[0]["external_id"] == "bad"
    assert rows[0]["node_id"] == node_id("webhook", "bad")
    assert "slug is empty" in rows[0]["detail"]["error"]
    assert [s["external_id"] for s in kb.stuck_ingest_records()] == ["bad"]


def test_drain_failure_without_an_external_id_keys_to_the_inbox_row(kb, pool):
    box = Inbox(pool)
    bad = box.append("webhook", external_id=None, event_type="delete", payload={})
    InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    rows = kb.ingest_log(event="ingest_failed")
    assert len(rows) == 1
    assert rows[0]["external_id"] == f"inbox#{bad}"
    assert rows[0]["node_id"] == node_id("webhook", f"inbox#{bad}")


def test_drain_success_after_failure_ends_the_streak(kb, pool):
    box = Inbox(pool)
    box.append("webhook", external_id="flaky", event_type="upsert",
               payload={"external_id": "flaky", "slug": "!!!", "content": "x"})
    InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert [s["external_id"] for s in kb.stuck_ingest_records()] == ["flaky"]
    box.append("webhook", external_id="flaky", event_type="upsert",
               payload={"external_id": "flaky", "title": "Flaky", "content": "ok"})
    InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert kb.get_by_path("Flaky") is not None
    assert kb.stuck_ingest_records() == []


def test_drain_resolves_via_landed_on_a_no_op_recovery_upsert(kb, pool):
    # A recovery push whose content is byte-for-byte unchanged writes no node event (the
    # no-op upsert gate), so without an explicit 'landed' resolve the record would stay
    # stuck forever despite having landed.
    box = Inbox(pool)
    payload = {"external_id": "flaky", "title": "Flaky", "content": "x"}
    box.append("webhook", external_id="flaky", event_type="upsert", payload=payload)
    InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())            # lands
    box.append("webhook", external_id="flaky", event_type="upsert",
               payload={"external_id": "flaky", "slug": "!!!", "content": "x"})
    InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())            # fails -> stuck
    assert [s["external_id"] for s in kb.stuck_ingest_records()] == ["flaky"]
    box.append("webhook", external_id="flaky", event_type="upsert", payload=payload)
    InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())            # no-op upsert
    assert kb.stuck_ingest_records() == []
    resolved = kb.ingest_log(event="ingest_resolved")
    assert len(resolved) == 1
    assert resolved[0]["external_id"] == "flaky"
    assert resolved[0]["detail"] == {"reason": "landed"}


def test_drain_log_ingest_failed_raising_does_not_abort_the_batch(kb, pool, monkeypatch):
    # Observability must never abort ingestion: a raise here must not skip mark_processed,
    # or the whole batch (including the good event) would be redelivered forever.
    box = Inbox(pool)
    box.append("webhook", external_id="bad", event_type="upsert",
               payload={"external_id": "bad", "slug": "!!!", "content": "x"})
    box.append("webhook", external_id="ok", event_type="upsert",
               payload={"external_id": "ok", "title": "OK", "content": "z"})

    def boom(*args, **kwargs):
        raise RuntimeError("db down")
    monkeypatch.setattr(kb, "log_ingest_failed", boom)

    res = InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert kb.get_by_path("OK") is not None                  # the good event still landed
    assert box.claim_pending("webhook") == []                 # both rows no longer pending
    assert any("log failed" in e for e in res.errors)


def test_drain_reports_changed_separately_from_seen(kb, pool):
    box = Inbox(pool)
    payload = {"external_id": "same", "title": "Same", "content": "unchanged"}
    box.append("webhook", external_id="same", event_type="upsert", payload=payload)
    first = InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert (first.upserted, first.changed) == (1, 1)
    box.append("webhook", external_id="same", event_type="upsert", payload=payload)
    again = InboxDrainService(kb, pool).drain("webhook", WebhookDecoder())
    assert (again.upserted, again.changed) == (1, 0)          # redelivery: nothing written
