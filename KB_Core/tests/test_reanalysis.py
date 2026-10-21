
from types import SimpleNamespace

from kb_core.analysis.reanalysis import ReanalysisWorker
from kb_core.domain import NodePatch, NodeRecord
from kb_core.identity import node_id


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


def _stale(pool, nid, days):
    with pool.connection() as conn:
        conn.execute("UPDATE nodes SET enrichment_status='done', "
                     f"last_analyzed_at = now() - interval '{days} days', "
                     f"last_gate_analyzed_at = now() - interval '{days} days' WHERE id=%s",
                     (nid,))


def _done(pool, *nids):
    """Mark nodes first-pass-enriched ('done') — the gate recrawl now requires."""
    with pool.connection() as conn:
        for nid in nids:
            conn.execute("UPDATE nodes SET enrichment_status='done' WHERE id=%s", (nid,))


def _hashes(kb):
    return {"class_hash": kb.active_class_hash(), "kind_hash": kb.doc_kind_vocab_hash()}


def _move_vocabulary(kb, name="pattern"):
    """Activate a class — the real reason a recrawl becomes due."""
    kb.upsert_entity_class(name, description="a recurring shape", active=True)


def _due_ids(kb, sources, limit=10):
    return [n.id for n, _ in kb.due_for_reanalysis(sources, limit=limit, **_hashes(kb))]


class _FakeEnricher:
    name = "fake"

    def __init__(self):
        self.seen = []

    def enrich(self, node, kb, *, only=None):
        self.seen.append(node.id)


def test_worker_initializes_changed_count():
    worker = ReanalysisWorker(object(), [], [], batch=10)
    assert worker.last_changed == 0


def test_hash_failure_fails_closed_for_one_tick(capsys):
    class _HashFailingKB:
        def active_class_hash(self):
            raise RuntimeError("hash unavailable")

    worker = ReanalysisWorker(_HashFailingKB(), [], ["agent"], batch=10)

    assert worker.run() == 0
    assert worker.last_changed == 0
    assert "dependency hash failed" in capsys.readouterr().out


def test_worker_records_skipped_pass_as_attempted_not_converged():
    class _CompletionRecordingKB:
        def __init__(self):
            self.completed = []

        def recrawl_fingerprint(self, node_id):
            return ()

        def mark_analyzed(self, node_id, **cursors):
            self.completed.append((node_id, cursors))

    class _SkippedPassEnricher:
        def enrich(self, node, kb, *, only=None):
            return {"passes": []}

    kb = _CompletionRecordingKB()
    worker = ReanalysisWorker(kb, [_SkippedPassEnricher()], ["agent"])

    worker._process([
        (SimpleNamespace(id="node-1"), {"extract_entities"},
         {"class_hash": "class-v2"}),
    ])

    assert kb.completed == [("node-1", {"attempted_class_hash": "class-v2"})]


def test_worker_failure_carries_the_gate_attempt_to_failure_completion():
    class _FailureRecordingKB:
        def __init__(self):
            self.failed = []

        def recrawl_fingerprint(self, node_id):
            return ()

        def mark_analysis_failed(self, node_id, **cursors):
            self.failed.append((node_id, cursors))

    kb = _FailureRecordingKB()
    worker = ReanalysisWorker(kb, [_ExplodingEnricher()], ["agent"])

    worker._process([(SimpleNamespace(id="node-1"), {"entity_gate"}, {"gate": True})])

    assert kb.failed == [("node-1", {"gate": True})]


def test_manual_recrawl_stamps_only_reported_passes_and_gate():
    class _ManualKB:
        def __init__(self):
            self.completed = []

        def crawlable_node(self, node_id, sources):
            return SimpleNamespace(id=node_id)

        def active_class_hash(self):
            return "class-v2"

        def doc_kind_vocab_hash(self):
            return "kind-v2"

        def recrawl_fingerprint(self, node_id):
            return ()

        def mark_analyzed(self, node_id, **cursors):
            self.completed.append((node_id, cursors))

    class _PartialReport:
        def enrich(self, node, kb, *, only=None):
            assert only is None
            return {"passes": ["entity_gate", "doc_kind"]}

    kb = _ManualKB()
    assert ReanalysisWorker(kb, [_PartialReport()], ["agent"]).run_node("node-1") == 1
    assert kb.completed == [(
        "node-1",
        {"attempted_class_hash": "class-v2", "kind_hash": "kind-v2", "gate": True},
    )]


def test_backfill_stamps_only_reported_passes_and_gate():
    class _BackfillKB:
        def __init__(self):
            self.completed = []
            self.queried = False

        def due_for_backfill(self, sources, *, before, limit):
            if self.queried:
                return []
            self.queried = True
            return [SimpleNamespace(id="node-1")]

        def active_class_hash(self):
            return "class-v2"

        def doc_kind_vocab_hash(self):
            return "kind-v2"

        def recrawl_fingerprint(self, node_id):
            return ()

        def mark_analyzed(self, node_id, **cursors):
            self.completed.append((node_id, cursors))

    class _PartialReport:
        def enrich(self, node, kb, *, only=None):
            assert only is None
            return {"passes": ["entity_gate", "extract_entities"]}

    kb = _BackfillKB()
    assert ReanalysisWorker(kb, [_PartialReport()], ["agent"]).backfill("before") == 1
    assert kb.completed == [(
        "node-1",
        {"class_hash": "class-v2", "attempted_kind_hash": "kind-v2", "gate": True},
    )]


def test_last_analyzed_at_column_exists(pool):
    with pool.connection() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'nodes'"
        ).fetchall()}
    assert "last_analyzed_at" in cols


def test_mark_analyzed_advances_cursor_without_touching_status(kb):
    n = kb.upsert(_mem("a"))                         # enrichment_status 'none'
    kb.mark_analyzed(n.id)
    got = kb.get(n.id)
    assert got.last_analyzed_at is not None
    assert got.enrichment_status == "none"           # re-analysis leaves ingest state alone


def test_idle_when_nothing_changed_since_last_analysis(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    assert _due_ids(kb, ["agent"]) == []


def test_stalest_first(kb, pool):
    a = kb.upsert(_mem("a"))
    b = kb.upsert(_mem("b"))
    _done(pool, a.id, b.id)
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    kb.mark_analyzed(b.id, gate=True, **_hashes(kb))
    _stale(pool, a.id, 10)
    _stale(pool, b.id, 1)
    _move_vocabulary(kb)                              # both become due
    assert _due_ids(kb, ["agent"], limit=1) == [a.id]  # one slot -> stalest


def test_only_listed_sources_are_crawled(kb, pool):
    a = kb.upsert(_mem("a"))
    w = kb.upsert(NodeRecord(source="webhook", external_id="w", type="note",
                             content="c", slug="w"))
    _done(pool, a.id, w.id)
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    kb.mark_analyzed(w.id, gate=True, **_hashes(kb))
    _move_vocabulary(kb)
    assert node_id("webhook", "w") not in set(_due_ids(kb, ["agent"]))


def test_worker_idles_when_nothing_due(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    assert ReanalysisWorker(kb, [_FakeEnricher()], ["agent"], batch=10).run() == 0


def _db_now(pool):
    with pool.connection() as conn:
        return conn.execute("SELECT now() AS now").fetchone()["now"]


def test_backfill_covers_all_existing_nodes_once_including_already_analyzed(kb, pool):
    a = kb.upsert(_mem("a"))
    b = kb.upsert(_mem("b"))
    _done(pool, a.id, b.id)                           # both first-pass enriched -> crawlable
    kb.mark_analyzed(a.id)                            # 'a' is already analyzed (recent)
    before = _db_now(pool)
    fake = _FakeEnricher()
    total = ReanalysisWorker(kb, [fake], ["agent"], batch=1).backfill(before)  # batch=1 -> loops
    assert total == 2                                # both, even the already-analyzed one
    assert set(fake.seen) == {a.id, b.id}


def test_backfill_ignores_nodes_created_after_start(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    before = _db_now(pool)
    b = kb.upsert(_mem("b"))                          # created AFTER the backfill start
    _done(pool, b.id)                                 # done, but still excluded by the time gate
    due_ids = {n.id for n in kb.due_for_backfill(["agent"], before=before, limit=10)}
    assert a.id in due_ids
    assert node_id("agent", "b") not in due_ids


def test_backfill_terminates_when_corpus_drained(kb, pool):
    ids = [kb.upsert(_mem(f"n{i}")).id for i in range(3)]
    _done(pool, *ids)
    before = _db_now(pool)
    total = ReanalysisWorker(kb, [_FakeEnricher()], ["agent"], batch=2).backfill(before)
    assert total == 3
    # everything is now analyzed past `before` -> a second backfill does nothing
    assert ReanalysisWorker(kb, [_FakeEnricher()], ["agent"], batch=2).backfill(before) == 0


def test_recrawl_skips_nodes_not_first_pass_enriched(kb, pool):
    # Only 'done' nodes are crawlable; pending/processing/failed/none are skipped so recrawl
    # never races the ingest pipeline on a node it's still enriching.
    done = kb.upsert(_mem("done"))
    for eid, status in [("pend", "pending"), ("proc", "processing"), ("fail", "failed")]:
        n = kb.upsert(_mem(eid))
        with pool.connection() as conn:
            conn.execute("UPDATE nodes SET enrichment_status=%s WHERE id=%s", (status, n.id))
    _done(pool, done.id)
    got = set(_due_ids(kb, ["agent"]))
    assert got == {done.id}


def test_backfill_skips_nodes_not_first_pass_enriched(kb, pool):
    done = kb.upsert(_mem("bdone"))
    kb.upsert(_mem("bpend"))                          # stays 'none' -> not crawlable
    _done(pool, done.id)
    before = _db_now(pool)
    got = {n.id for n in kb.due_for_backfill(["agent"], before=before, limit=10)}
    assert got == {done.id}


# --- Manual re-crawl trigger (run_node / request_reanalysis) -----------------------------

def test_run_node_recrawls_only_the_targeted_node(kb, pool):
    a = kb.upsert(_mem("a"))
    b = kb.upsert(_mem("b"))
    _done(pool, a.id, b.id)                           # both crawlable
    fake = _FakeEnricher()
    n = ReanalysisWorker(kb, [fake], ["agent"], batch=10).run_node(a.id)
    assert n == 1
    assert fake.seen == [a.id]                        # only the targeted node ran
    assert kb.get(a.id).last_analyzed_at is not None  # its cursor advanced
    assert kb.get(b.id).last_analyzed_at is None      # the other node untouched


def test_run_node_ignores_the_staleness_gate(kb, pool):
    # A just-analyzed node is NOT due for the periodic pass, but a manual re-crawl runs it
    # anyway — the user asked for it explicitly.
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    assert _due_ids(kb, ["agent"]) == []
    rec = _PassRecordingEnricher()
    assert ReanalysisWorker(kb, [rec], ["agent"], batch=10).run_node(a.id) == 1
    assert rec.calls == [(a.id, None)]


def test_run_node_skips_nodes_not_first_pass_enriched(kb):
    a = kb.upsert(_mem("a"))                          # status 'none' -> not crawlable
    fake = _FakeEnricher()
    assert ReanalysisWorker(kb, [fake], ["agent"], batch=10).run_node(a.id) == 0
    assert fake.seen == []


def test_run_node_skips_retired_memories(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    kb.mark_dead(a.id)                                # retired -> frozen, never recrawled
    fake = _FakeEnricher()
    assert ReanalysisWorker(kb, [fake], ["agent"], batch=10).run_node(a.id) == 0
    assert fake.seen == []


def test_request_reanalysis_emits_wake_notification(kb):
    import os

    import psycopg

    dsn = os.environ.get("KB_TEST_DATABASE_URL",
                         "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test")
    a = kb.upsert(_mem("a"))
    with psycopg.connect(dsn, autocommit=True) as listen:
        listen.execute("LISTEN kb_reanalyze")
        kb.request_reanalysis(a.id)                   # NOTIFY kb_reanalyze, '<id>'
        notes = list(listen.notifies(timeout=5, stop_after=1))
    assert [n.payload for n in notes] == [str(a.id)]


# --- Dependency-gated recrawl: pass selection, cursor stamping, failure handling ---------

class _PassRecordingEnricher:
    """Captures the `only` set each node was enriched with."""
    name = "recording"

    def __init__(self):
        self.calls = []

    def enrich(self, node, kb, *, only=None):
        self.calls.append((node.id, only))


def test_worker_runs_only_the_pass_whose_dependency_moved(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    _move_vocabulary(kb)                              # class vocab only

    rec = _PassRecordingEnricher()
    ReanalysisWorker(kb, [rec], ["agent"], batch=10).run()

    assert rec.calls == [(a.id, {"extract_entities"})]


def test_worker_runs_the_union_when_several_reasons_fire(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    # Stamp only the class cursor: the class move, the stale clock, and the never-stamped
    # kind cursor then exercise the three independent reasons in one worker run.
    kb.mark_analyzed(a.id, class_hash=kb.active_class_hash())
    _stale(pool, a.id, 99)                            # gate clock fires too
    _move_vocabulary(kb)

    rec = _PassRecordingEnricher()
    ReanalysisWorker(kb, [rec], ["agent"], batch=10).run()

    (_, only), = rec.calls
    assert only == {"entity_gate", "extract_entities", "doc_kind"}


def test_worker_stamps_only_the_cursors_for_passes_it_ran(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    _move_vocabulary(kb)
    expected = kb.active_class_hash()

    ReanalysisWorker(kb, [_PassRecordingEnricher()], ["agent"], batch=10).run()

    with pool.connection() as conn:
        row = conn.execute("SELECT analyzed_class_hash FROM nodes WHERE id=%s",
                           (a.id,)).fetchone()
    assert row["analyzed_class_hash"] == expected
    assert kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)) == []   # now settled


class _ExplodingEnricher:
    name = "boom"

    def enrich(self, node, kb, *, only=None):
        raise RuntimeError("boom")


def test_failed_enrichment_advances_the_clock_but_not_the_cursors(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    old_class_hash = kb.active_class_hash()
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    _move_vocabulary(kb)
    failed_class_hash = kb.active_class_hash()
    before = kb.get(a.id).last_analyzed_at

    ReanalysisWorker(kb, [_ExplodingEnricher()], ["agent"], batch=10).run()

    assert kb.get(a.id).last_analyzed_at > before
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT analyzed_class_hash, attempted_class_hash FROM nodes WHERE id=%s",
            (a.id,)).fetchone()
    assert row["analyzed_class_hash"] == old_class_hash
    assert row["attempted_class_hash"] == failed_class_hash
    # Not hot-looped under the same dependency hash, but still not recorded as converged.
    assert kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)) == []
    _move_vocabulary(kb, "principle")
    assert [n.id for n, _ in kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb))] \
        == [a.id]                              # retries when the dependency moves again


def test_skipped_extraction_retries_after_entity_gate_reenables_node(kb, pool):
    class _SkippedExtraction:
        def enrich(self, node, kb, *, only=None):
            assert only == {"extract_entities"}
            return {"passes": []}

    a = kb.upsert(_mem("skipped"))
    _done(pool, a.id)
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    kb.set_entity_eligible(a.id, False, reason="noise")
    _move_vocabulary(kb)
    moved_hash = kb.active_class_hash()

    assert ReanalysisWorker(kb, [_SkippedExtraction()], ["agent"]).run() == 1
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT analyzed_class_hash, attempted_class_hash FROM nodes WHERE id=%s",
            (a.id,)).fetchone()
    assert row["analyzed_class_hash"] != moved_hash
    assert row["attempted_class_hash"] == moved_hash
    assert _due_ids(kb, ["agent"]) == []

    kb.set_entity_eligible(a.id, True)

    due = dict((n.id, reasons) for n, reasons in
               kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)))
    assert due[a.id].due_class is True


def test_changed_counter_distinguishes_a_no_op_sweep(kb, pool):
    """The signal that says whether a sweep earned its tokens."""
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    _stale(pool, a.id, 99)

    w = ReanalysisWorker(kb, [_FakeEnricher()], ["agent"], batch=10)
    assert w.run() == 1
    assert w.last_changed == 0          # _FakeEnricher writes nothing


class _KindWritingEnricher:
    name = "kindwriter"

    def enrich(self, node, kb, *, only=None):
        kb.update(node.id, NodePatch(doc_kind="design spec"))


def test_changed_counter_counts_a_productive_sweep(kb, pool):
    """A real fingerprint change must make the yield signal non-zero."""
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    _stale(pool, a.id, 99)

    w = ReanalysisWorker(kb, [_KindWritingEnricher()], ["agent"], batch=10)
    assert w.run() == 1
    assert w.last_changed == 1


def test_worker_runs_enrichers_and_advances_cursor(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    _stale(pool, a.id, 99)
    before = kb.get(a.id).last_analyzed_at
    fake = _FakeEnricher()

    n = ReanalysisWorker(kb, [fake], ["agent"], batch=10).run()

    assert n >= 1 and a.id in fake.seen
    got = kb.get(a.id)
    assert got.last_analyzed_at > before
    assert got.enrichment_status == "done"


class _OfflineEnricher:
    """Every pass rides a provider that is down (the tenant's kb-agent is offline)."""
    name = "offline"

    def __init__(self):
        self.calls = 0

    def enrich(self, node, kb, *, only=None):
        from datetime import datetime, timedelta, timezone

        from kb_core.ingest.inference import ProviderUnavailable
        self.calls += 1
        raise ProviderUnavailable("no agent answered", provider="agent",
                                  until=datetime.now(timezone.utc) + timedelta(minutes=10))


def _analysis_state(pool, nid):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT last_analyzed_at, analyzed_class_hash, attempted_class_hash "
            "FROM nodes WHERE id=%s", (nid,)).fetchone()


def test_provider_outage_leaves_the_node_due_and_pauses_the_lane(kb, pool):
    """An outage says nothing about the node: it is NOT marked failed (no cursor or clock
    moves, so it stays due), the provider is paused, the rest of the batch is not tried,
    and a tick while the pause holds makes no model call at all."""
    a = kb.upsert(_mem("a"))
    b = kb.upsert(_mem("b"))
    _done(pool, a.id, b.id)
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    kb.mark_analyzed(b.id, gate=True, **_hashes(kb))
    _move_vocabulary(kb)
    before = {nid: _analysis_state(pool, nid) for nid in (a.id, b.id)}
    offline = _OfflineEnricher()
    worker = ReanalysisWorker(kb, [offline], ["agent"], batch=10, gate_providers=("agent",))

    assert worker.run() == 0
    assert offline.calls == 1                                  # stopped after the outage
    assert {nid: _analysis_state(pool, nid) for nid in (a.id, b.id)} == before
    assert set(_due_ids(kb, ["agent"])) == {a.id, b.id}       # both still due
    assert kb.provider_paused("agent") is not None

    assert worker.run() == 0                                   # paused: no model call
    assert offline.calls == 1


def test_a_provider_learned_from_an_outage_is_gated_without_configuration(kb, pool):
    a = kb.upsert(_mem("a"))
    _done(pool, a.id)
    _stale(pool, a.id, 99)
    offline = _OfflineEnricher()
    worker = ReanalysisWorker(kb, [offline], ["agent"], batch=10)
    worker.run()
    assert _due_ids(kb, ["agent"]) == [a.id]                  # still due ...
    worker.run()
    assert offline.calls == 1                                  # ... but not retried while paused


def test_backfill_stops_on_a_provider_outage_instead_of_spinning(kb, pool):
    ids = [kb.upsert(_mem(f"o{i}")).id for i in range(3)]
    _done(pool, *ids)
    before = _db_now(pool)
    offline = _OfflineEnricher()
    total = ReanalysisWorker(kb, [offline], ["agent"], batch=1).backfill(before)
    assert total == 0 and offline.calls == 1
