from datetime import datetime, timedelta, timezone

from kb_core.domain import NodeRecord


def _rec(**over):
    base = dict(source="agent", external_id="m1", type="note",
                content="hello world", slug="hello", parent_path="")
    base.update(over)
    return NodeRecord(**base)


def test_no_pause_by_default(kb):
    assert kb.provider_paused("claude-cli") is None
    assert kb.active_pauses() == []


def test_pause_with_explicit_until_round_trips(kb):
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    kb.pause_provider("claude-cli", until=until, reason="session limit")
    assert kb.provider_paused("claude-cli") == until
    [p] = kb.active_pauses()
    assert (p["provider"], p["until"], p["reason"]) == ("claude-cli", until, "session limit")


def test_pause_without_until_uses_default_reprobe_interval(kb):
    kb.pause_provider("claude-cli", reason="auth outage")
    delta = kb.provider_paused("claude-cli") - datetime.now(timezone.utc)
    assert timedelta(minutes=13) < delta <= timedelta(minutes=15)


def test_pause_upsert_keeps_the_later_until(kb):
    late = datetime.now(timezone.utc) + timedelta(hours=8)
    kb.pause_provider("claude-cli", until=late, reason="weekly limit")
    kb.pause_provider("claude-cli", reason="auth outage")   # +15min must NOT shorten
    assert kb.provider_paused("claude-cli") == late


def test_expired_pause_is_inactive(kb):
    kb.pause_provider("claude-cli",
                      until=datetime.now(timezone.utc) - timedelta(minutes=1), reason="over")
    assert kb.provider_paused("claude-cli") is None
    assert kb.active_pauses() == []


def test_defer_enrichment_returns_node_to_pending_and_pauses(kb):
    n = kb.upsert(_rec(external_id="d1", slug="d1", title="T"))
    kb.flag_stale_enrichment()
    [claimed] = kb.claim_enrichment(1)
    assert claimed.id == n.id

    kb.defer_enrichment(claimed.id, provider="claude-cli",
                        reason="InferenceError: claude CLI exited 1: session limit")

    again = kb.get(claimed.id)
    assert again.enrichment_status == "pending"          # back in the queue
    assert again.enriched_hash != again.content_hash     # NOT pinned terminal
    deferred = [r for r in kb.ingest_log() if r["event"] == "enrichment_deferred"]
    assert [r["node_id"] for r in deferred] == [claimed.id]
    assert deferred[0]["detail"]["provider"] == "claude-cli"
    assert "session limit" in deferred[0]["detail"]["error"]
    assert kb.provider_paused("claude-cli") is not None  # lane gate armed
    failed = [r for r in kb.ingest_log() if r["event"] == "enrichment_failed"]
    assert failed == []                                  # a deferral is not a failure
