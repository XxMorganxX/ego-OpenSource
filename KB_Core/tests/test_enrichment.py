import threading
from types import SimpleNamespace

from kb_core.domain import NodePatch, NodeRecord
from kb_core.ingest.enrichment import Enricher, EnrichmentWorker


def _rec(**over):
    base = dict(source="agent", external_id="m1", type="note",
                content="hello world", slug="hello", parent_path="")
    base.update(over)
    return NodeRecord(**base)


class _SummaryEnricher:
    name = "summary"

    def enrich(self, node, kb):
        kb.update(node.id, NodePatch(summary=f"summary of {node.title}"))


class _PassReportingEnricher:
    name = "pass-reporting"

    def enrich(self, node, kb):
        return {"passes": ["entity_gate", "extract_entities", "doc_kind"]}


class _Boom:
    name = "boom"

    def enrich(self, node, kb):
        raise RuntimeError("nope")


def test_worker_stamps_only_passes_the_enricher_reports_as_executed():
    class _RecordingKB:
        def __init__(self):
            self.completion = None

        def mark_enriched(self, node_id, **detail):
            self.completion = (node_id, detail)

    class _PartialReport:
        def enrich(self, node, kb):
            return {"passes": ["entity_gate", "doc_kind"]}

    kb = _RecordingKB()
    EnrichmentWorker(kb, [_PartialReport()])._process(
        SimpleNamespace(id="node-1"), class_hash="class-v2", kind_hash="kind-v2")

    assert kb.completion == (
        "node-1",
        {"class_hash": None, "kind_hash": "kind-v2", "gate": True, "detail": None},
    )


def test_worker_enriches_pending_nodes(kb, pool):
    n = kb.upsert(_rec(external_id="w1", slug="w1", title="T", content="c"))
    kb.flag_stale_enrichment()
    assert isinstance(_SummaryEnricher(), Enricher)
    processed = EnrichmentWorker(kb, [_SummaryEnricher()]).run()
    assert processed == 1
    done = kb.get(n.id)
    assert done.enrichment_status == "done" and done.summary == "summary of T"
    assert done.enriched_hash == done.content_hash
    assert EnrichmentWorker(kb, [_SummaryEnricher()]).run() == 0     # nothing left pending


def test_successful_full_enrichment_stamps_current_dependency_cursors(kb, pool):
    n = kb.upsert(_rec(external_id="cursor", slug="cursor", content="c"))
    kb.flag_stale_enrichment()
    expected_class = kb.active_class_hash()
    expected_kind = kb.doc_kind_vocab_hash()

    EnrichmentWorker(kb, [_PassReportingEnricher()]).run()

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT analyzed_class_hash, analyzed_kind_hash, last_gate_analyzed_at "
            "FROM nodes WHERE id=%s",
            (n.id,)).fetchone()
    assert row["analyzed_class_hash"] == expected_class
    assert row["analyzed_kind_hash"] == expected_kind
    assert row["last_gate_analyzed_at"] is not None


def test_worker_logs_enrichment_done(kb, pool):
    n = kb.upsert(_rec(external_id="w3", slug="w3", title="T", content="c"))
    kb.flag_stale_enrichment()
    EnrichmentWorker(kb, [_SummaryEnricher()]).run()
    done = [r for r in kb.ingest_log() if r["event"] == "enrichment_done"]
    assert [r["node_id"] for r in done] == [n.id]


def test_worker_actually_overlaps_nodes_when_concurrent(kb, pool):
    # A rendezvous that can only clear if two nodes are genuinely in flight at once. A serial
    # loop blocks the first node until the barrier times out, so this fails loudly instead of
    # passing against a fake-parallel implementation.
    barrier = threading.Barrier(2, timeout=15)

    class _Meet:
        name = "meet"

        def enrich(self, node, kb):
            barrier.wait()

    ids = [kb.upsert(_rec(external_id=f"c{i}", slug=f"c{i}", content="c")).id
           for i in range(2)]
    kb.flag_stale_enrichment()
    assert EnrichmentWorker(kb, [_Meet()], concurrency=2).run() == 2
    # Both cleared the rendezvous -> both ran at the same time. Serial would leave them
    # 'failed' with BrokenBarrierError.
    assert [kb.get(i).enrichment_status for i in ids] == ["done", "done"]


def test_concurrent_failure_is_isolated_to_its_own_node(kb, pool):
    class _BoomOnOne:
        name = "boom-on-one"

        def enrich(self, node, kb):
            if node.slug == "bad":
                raise RuntimeError("nope")
            kb.update(node.id, NodePatch(summary="ok"))

    good = kb.upsert(_rec(external_id="ok1", slug="ok1", content="c")).id
    bad = kb.upsert(_rec(external_id="bad", slug="bad", content="c")).id
    kb.flag_stale_enrichment()
    assert EnrichmentWorker(kb, [_BoomOnOne()], concurrency=2).run() == 2
    assert kb.get(good).enrichment_status == "done"      # sibling unaffected
    assert kb.get(bad).enrichment_status == "failed"


def test_default_is_serial_and_still_drains_the_batch(kb, pool):
    # The default must stay the loop this always ran -- concurrency is strictly opt-in.
    ids = [kb.upsert(_rec(external_id=f"s{i}", slug=f"s{i}", content="c")).id
           for i in range(3)]
    kb.flag_stale_enrichment()
    worker = EnrichmentWorker(kb, [_SummaryEnricher()])
    assert worker._concurrency == 1
    assert worker.run() == 3
    assert all(kb.get(i).enrichment_status == "done" for i in ids)


def test_worker_marks_failed_on_error(kb, pool):
    n = kb.upsert(_rec(external_id="w2", slug="w2", content="c"))
    kb.flag_stale_enrichment()
    EnrichmentWorker(kb, [_Boom()]).run()
    assert kb.get(n.id).enrichment_status == "failed"
    # the failure is observable in the ingest log, with the reason captured in detail
    failed = kb.ingest_log(event="enrichment_failed")
    assert [r["node_id"] for r in failed] == [n.id]
    assert "nope" in failed[0]["detail"]["error"]


def test_retry_failed_re_drives_the_fail_pile(kb, pool):
    n = kb.upsert(_rec(external_id="w4", slug="w4", title="T", content="c"))
    kb.flag_stale_enrichment()
    EnrichmentWorker(kb, [_Boom()]).run()                          # fails -> pinned 'failed'
    assert kb.get(n.id).enrichment_status == "failed"
    # terminal: flag_stale won't re-queue it (enriched_hash == content_hash), so a worker
    # pass over a quiet KB does nothing — the failure is stranded.
    kb.flag_stale_enrichment()
    assert EnrichmentWorker(kb, [_SummaryEnricher()]).run() == 0
    # refresh the pile -> the node is re-attempted and now succeeds.
    assert kb.retry_failed_enrichment() == 1
    assert EnrichmentWorker(kb, [_SummaryEnricher()]).run() == 1
    assert kb.get(n.id).enrichment_status == "done"
    assert kb.retry_failed_enrichment() == 0                       # nothing left failed


def test_retry_failed_scoped_to_node_ids(kb, pool):
    a = kb.upsert(_rec(external_id="r1", slug="r1", content="c"))
    b = kb.upsert(_rec(external_id="r2", slug="r2", content="c"))
    kb.flag_stale_enrichment()
    EnrichmentWorker(kb, [_Boom()]).run()                          # both -> 'failed'
    assert kb.get(a.id).enrichment_status == "failed"
    assert kb.get(b.id).enrichment_status == "failed"
    # retry only node a -> b is left stranded (the dashboard uses this to skip terminal failures)
    assert kb.retry_failed_enrichment(node_ids=[a.id]) == 1
    assert kb.get(a.id).enrichment_status == "pending"
    assert kb.get(b.id).enrichment_status == "failed"
    # an empty subset re-queues nothing — it must not fall back to re-queuing everything
    assert kb.retry_failed_enrichment(node_ids=[]) == 0
    assert kb.get(b.id).enrichment_status == "failed"


class _ReportingEnricher:
    name = "reporting"

    def __init__(self, providers):
        self._providers = providers

    def enrich(self, node, kb):
        return {"providers": self._providers}


def test_worker_records_merged_providers_in_done_detail(kb, pool):
    n = kb.upsert(_rec(external_id="w9", slug="w9", title="T", content="c"))
    kb.flag_stale_enrichment()
    EnrichmentWorker(kb, [
        _ReportingEnricher(["claude-cli:claude-sonnet-5"]),
        _ReportingEnricher(["local:gemma4:26b", "claude-cli:claude-sonnet-5"]),
        _SummaryEnricher(),                     # returns None -> contributes nothing
    ]).run()
    done = [r for r in kb.ingest_log() if r["event"] == "enrichment_done"]
    assert [r["node_id"] for r in done] == [n.id]
    assert done[0]["detail"] == {
        "providers": ["claude-cli:claude-sonnet-5", "local:gemma4:26b"]}


def test_worker_logs_no_detail_when_no_enricher_reports(kb, pool):
    kb.upsert(_rec(external_id="w10", slug="w10", title="T", content="c"))
    kb.flag_stale_enrichment()
    EnrichmentWorker(kb, [_SummaryEnricher()]).run()
    done = [r for r in kb.ingest_log() if r["event"] == "enrichment_done"]
    assert done[0]["detail"] == {}              # the column's NOT NULL DEFAULT shape, as before


def test_worker_ignores_a_non_list_providers_report(kb, pool):
    # A bare-string providers value must be ignored outright — iterating it would
    # shred it into single-character "providers" in the log.
    kb.upsert(_rec(external_id="w11", slug="w11", title="T", content="c"))
    kb.flag_stale_enrichment()
    EnrichmentWorker(kb, [_ReportingEnricher("claude-cli:x")]).run()
    done = [r for r in kb.ingest_log() if r["event"] == "enrichment_done"]
    assert done[0]["detail"] == {}


from kb_core.ingest.inference import ProviderUnavailable


class _ProviderDown:
    name = "provider-down"

    def enrich(self, node, kb):
        raise ProviderUnavailable(
            "claude CLI exited 1: You've hit your session limit · resets 5:30am",
            provider="claude-cli")


def test_worker_defers_on_provider_unavailable(kb, pool):
    n = kb.upsert(_rec(external_id="pd1", slug="pd1", title="T"))
    kb.flag_stale_enrichment()
    assert EnrichmentWorker(kb, [_ProviderDown()]).run() == 1
    node = kb.get(n.id)
    assert node.enrichment_status == "pending"           # deferred, not failed
    assert kb.provider_paused("claude-cli") is not None  # pause armed for the lane
    events = {r["event"] for r in kb.ingest_log()}
    assert "enrichment_deferred" in events
    assert "enrichment_failed" not in events


class _AgentOffline:
    """Every call is an outage (the tenant's kb-agent is offline); counts the calls, each
    of which would be a full agent timeout in production."""
    name = "agent-offline"

    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def enrich(self, node, kb):
        with self._lock:
            self.calls += 1
        raise ProviderUnavailable("no agent answered", provider="agent")


def _claimable(kb, n, prefix):
    ids = [kb.upsert(_rec(external_id=f"{prefix}{i}", slug=f"{prefix}{i}", title="T")).id
           for i in range(n)]
    kb.flag_stale_enrichment()
    return ids


def test_an_outage_ends_the_serial_batch_and_hands_the_rest_back(kb, pool):
    """One outage = one timeout, not one per claimed node: after the first
    ProviderUnavailable every remaining claimed node goes back to 'pending' untouched."""
    ids = _claimable(kb, 4, "so")
    offline = _AgentOffline()
    assert EnrichmentWorker(kb, [offline]).run() == 1  # nodes TRIED, not claimed
    assert offline.calls == 1
    assert {kb.get(i).enrichment_status for i in ids} == {"pending"}
    assert kb.provider_paused("agent") is not None
    deferred = [r for r in kb.ingest_log() if r["event"] == "enrichment_deferred"]
    assert len(deferred) == 1                            # the rest were never tried


def test_an_outage_ends_the_pooled_batch_too(kb, pool):
    ids = _claimable(kb, 6, "po")
    offline = _AgentOffline()
    n = EnrichmentWorker(kb, [offline], concurrency=2).run()
    assert offline.calls <= 2                            # at most the ones already in flight
    assert n == offline.calls                            # nodes tried, not claimed
    assert {kb.get(i).enrichment_status for i in ids} == {"pending"}
    assert kb.provider_paused("agent") is not None


def test_a_gated_provider_paused_before_a_node_starts_is_not_called(kb, pool):
    from datetime import datetime, timedelta, timezone

    ids = _claimable(kb, 3, "pp")
    kb.pause_provider("agent", until=datetime.now(timezone.utc) + timedelta(minutes=10),
                      reason="offline")
    offline = _AgentOffline()
    EnrichmentWorker(kb, [offline], gate_providers=("agent",)).run()
    assert offline.calls == 0
    assert {kb.get(i).enrichment_status for i in ids} == {"pending"}


def _pause_read_breaks(monkeypatch, kb):
    def boom(self, provider):
        raise RuntimeError("PoolTimeout: couldn't get a connection after 30.00 sec")
    monkeypatch.setattr(type(kb), "provider_paused", boom)


def test_a_failing_pause_check_fails_open_on_the_serial_path(kb, pool, monkeypatch, capsys):
    """The per-node pause read is advisory: if it raises (a transient pool timeout), the
    node proceeds exactly as before the gate existed. The worker never raises, and no
    claimed node is stranded at 'processing'."""
    ids = _claimable(kb, 3, "fo")
    _pause_read_breaks(monkeypatch, kb)
    assert EnrichmentWorker(kb, [_SummaryEnricher()], gate_providers=("agent",)).run() == 3
    assert {kb.get(i).enrichment_status for i in ids} == {"done"}
    assert capsys.readouterr().out.count("pause check failed") == 1   # logged once


def test_a_failing_pause_check_fails_open_in_the_pool(kb, pool, monkeypatch):
    ids = _claimable(kb, 4, "fp")
    _pause_read_breaks(monkeypatch, kb)
    worker = EnrichmentWorker(kb, [_SummaryEnricher()], concurrency=2,
                              gate_providers=("agent",))
    assert worker.run() == 4
    assert {kb.get(i).enrichment_status for i in ids} == {"done"}


def test_a_failing_hand_back_processes_the_node_instead_of_stranding_it(kb, pool, monkeypatch):
    from datetime import datetime, timedelta, timezone

    ids = _claimable(kb, 2, "fh")
    kb.pause_provider("agent", until=datetime.now(timezone.utc) + timedelta(minutes=10),
                      reason="offline")

    def boom(self, id):
        raise RuntimeError("PoolTimeout")
    monkeypatch.setattr(type(kb), "release_enrichment_claim", boom)
    assert EnrichmentWorker(kb, [_SummaryEnricher()], gate_providers=("agent",)).run() == 2
    assert {kb.get(i).enrichment_status for i in ids} == {"done"}
