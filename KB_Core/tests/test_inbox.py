from kb_core.ingest.inbox import Inbox


def test_append_and_claim_fifo(kb, pool):
    box = Inbox(pool)
    i1 = box.append("webhook", external_id="a", event_type="upsert", payload={"x": 1})
    box.append("webhook", external_id="b", event_type="delete", payload={})
    box.append("other", external_id="c", event_type="upsert", payload={})
    events = box.claim_pending("webhook")
    assert [e.id for e in events] == [i1, i1 + 1]              # only this source, in order
    assert events[0].payload == {"x": 1} and events[1].event_type == "delete"


def test_mark_processed_and_failed_leave_pending(kb, pool):
    box = Inbox(pool)
    a = box.append("webhook", external_id="a", event_type="upsert", payload={})
    b = box.append("webhook", external_id="b", event_type="upsert", payload={})
    box.mark_processed([a])
    box.mark_failed(b, "boom")
    assert box.claim_pending("webhook") == []                 # both left the pending set
