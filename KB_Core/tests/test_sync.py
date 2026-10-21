import pytest

from kb_core.adapters.obsidian.adapter import ObsidianAdapter
from kb_core.domain import NodeRecord
from kb_core.sync import SyncService


class _RecordingIngestor:
    """A custom ingestor that records what it persisted, then delegates to the core."""

    def __init__(self):
        self.calls: list[str] = []

    def upsert(self, kb, record):
        self.calls.append(record.external_id)
        return kb.upsert(record)


class _StubAdapter:
    """A minimal in-memory adapter; sets `ingestor` only when one is supplied."""

    source = "stub"

    def __init__(self, records, ingestor=None):
        self._records = records
        if ingestor is not None:
            self.ingestor = ingestor

    def fetch(self):
        yield from self._records


def _stub_record(eid="1", slug="hi"):
    return NodeRecord(source="stub", external_id=eid, type="note",
                      content="hi", slug=slug)


def _vault(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_sync_creates_nodes(tmp_path, kb, pool):
    vault = _vault(tmp_path, {"Projects/design.md": "# Design\nbody"})
    res = SyncService(kb, pool).sync(ObsidianAdapter(vault))
    assert res.upserted == 1
    assert kb.get_by_path("Projects.design") is not None


def test_sync_prunes_deleted_file(tmp_path, kb, pool):
    vault = _vault(tmp_path, {"a.md": "aaa", "b.md": "bbb"})
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(vault))
    (vault / "b.md").unlink()
    res = svc.sync(ObsidianAdapter(vault))
    assert res.deleted == 1
    assert kb.get_by_path("a") is not None
    assert kb.get_by_path("b") is None


def test_sync_dry_run_writes_nothing(tmp_path, kb, pool):
    vault = _vault(tmp_path, {"a.md": "aaa"})
    res = SyncService(kb, pool).sync(ObsidianAdapter(vault), dry_run=True)
    assert res.upserted == 1
    assert kb.get_by_path("a") is None


def test_sync_missing_vault_raises_before_prune(tmp_path, kb, pool):
    vault = _vault(tmp_path, {"keep.md": "k"})
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(vault))
    with pytest.raises(FileNotFoundError):
        svc.sync(ObsidianAdapter(tmp_path / "gone"))
    assert kb.get_by_path("keep") is not None


def test_sync_reusing_the_same_adapter_does_not_double_count_stale_errors(tmp_path, kb, pool):
    """A regression guard for adapter reuse in KB_MCP (found in review).

    KB_MCP's build_server holds ONE Adapter instance per source for the life of the
    process (main() builds it once; every sync_source call reuses it) -- unlike every
    other test in this file, which constructs a fresh ObsidianAdapter per sync() call.
    SyncService.sync captures `adapter.errors` TWICE: once right after prepare() (its
    own comment: "captured now, before fetch() resets adapter.errors") and again after
    the fetch loop. That design silently assumes `adapter.errors` is empty when
    prepare() runs -- true for a fresh adapter, false for a REUSED one whose previous
    fetch() left errors sitting on `self.errors` (fetch() only resets the list at its
    own start, and prepare() didn't reset it at all before this fix). Reproduced here
    with NO concurrency at all -- two sequential sync() calls on one adapter instance
    over a vault of persistently-malformed notes: without ObsidianAdapter.prepare()
    resetting self.errors first, call 2 reports double (2N) what call 1 correctly
    reports (N), and it keeps compounding on call 3, 4, ... This is what made the
    concurrent race in KB_MCP/tests/test_server.py look like more than a pure
    interleaving bug -- the two effects are independent and both needed fixing."""
    n = 5
    vault = tmp_path / "v"
    vault.mkdir()
    for i in range(n):
        (vault / f"bad_{i}.md").write_text("---\ntitle: [unterminated\n---\nbody",
                                           encoding="utf-8")
    adapter = ObsidianAdapter(vault)          # ONE instance, reused below -- not rebuilt
    svc = SyncService(kb, pool)
    r1 = svc.sync(adapter)
    r2 = svc.sync(adapter)
    r3 = svc.sync(adapter)
    assert len(r1.errors) == n
    assert len(r2.errors) == n
    assert len(r3.errors) == n


def test_sync_recomputes_importance_for_source(tmp_path, kb, pool):
    vault = _vault(tmp_path, {"n.md": "body"})
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(vault))
    node = kb.get_by_path("n")
    with pool.connection() as conn:                   # simulate accumulated usage
        conn.execute("UPDATE nodes SET access_count = 10 WHERE id = %s", (node.id,))
    svc.sync(ObsidianAdapter(vault))                  # re-sync should recompute
    assert kb.get_by_path("n").importance == 1.0 + 0.5 * (10 / 20)   # 1.25


# --- the optional per-source ingest seam ------------------------------------

def test_sync_uses_adapter_custom_ingestor(kb, pool):
    ingestor = _RecordingIngestor()
    adapter = _StubAdapter([_stub_record()], ingestor=ingestor)
    res = SyncService(kb, pool).sync(adapter, prune=False)
    assert res.upserted == 1
    assert ingestor.calls == ["1"]                    # the source's ingestor did the write
    assert kb.get_by_path("hi") is not None


def test_sync_dry_run_never_invokes_ingestor(kb, pool):
    ingestor = _RecordingIngestor()
    adapter = _StubAdapter([_stub_record()], ingestor=ingestor)
    res = SyncService(kb, pool).sync(adapter, prune=False, dry_run=True)
    assert res.upserted == 1
    assert ingestor.calls == []                       # dry-run counts but never persists
    assert kb.get_by_path("hi") is None


def test_sync_falls_back_to_default_ingestor_when_source_defines_none(kb, pool):
    adapter = _StubAdapter([_stub_record()])          # no `ingestor` attribute
    assert not hasattr(adapter, "ingestor")
    res = SyncService(kb, pool).sync(adapter, prune=False)
    assert res.upserted == 1
    assert kb.get_by_path("hi") is not None


def test_obsidian_adapter_is_core_only(tmp_path):
    # The reference vault adapter defines no ingest.py, so it has no custom ingestor.
    assert not hasattr(ObsidianAdapter(tmp_path), "ingestor")


def test_sync_flags_enrichment_for_new_nodes(tmp_path, kb, pool):
    vault = _vault(tmp_path, {"n.md": "body text"})
    SyncService(kb, pool).sync(ObsidianAdapter(vault))
    assert kb.get_by_path("n").enrichment_status == "pending"   # FlagEnrichment post-stage ran


def test_enabling_root_label_reparents_in_place_losslessly(tmp_path, kb, pool):
    # A vault first ingested flat, then re-synced under an "Obsidian" layer. Because the
    # layer only changes placement (not external_id -> not node id), the re-sync must move
    # each note IN PLACE: same id, new path, and any FK-referenced rows (edges, and likewise
    # entity_links) survive — no prune+recreate.
    vault = _vault(tmp_path, {"TeamHub/Foo.md": "# Foo\nbody", "TeamHub/Bar.md": "# Bar\nb2"})
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(vault))
    foo, bar = kb.get_by_path("TeamHub.Foo"), kb.get_by_path("TeamHub.Bar")
    assert foo and bar
    with pool.connection() as c:   # CASCADE-on-delete edge: gone iff the node was recreated
        c.execute("INSERT INTO edges (source_id, target_id, relation_type) "
                  "VALUES (%s, %s, 'relates_to')", (foo.id, bar.id))

    res = svc.sync(ObsidianAdapter(vault, root_label="Obsidian"))

    moved = kb.get_by_path("Obsidian.TeamHub.Foo")
    assert moved is not None and moved.id == foo.id            # same identity, new path
    assert moved.external_id == foo.external_id                # external_id untouched
    assert kb.get_by_path("TeamHub.Foo") is None                 # old path vacated
    assert kb.get_by_path("TeamHub") is None                     # emptied top folder reclaimed
    assert kb.get_by_path("Obsidian") is not None             # the layer now exists
    assert res.deleted == 0                                    # nothing pruned as a node
    with pool.connection() as c:
        survived = c.execute("SELECT count(*) n FROM edges WHERE source_id=%s AND target_id=%s",
                             (foo.id, bar.id)).fetchone()["n"]
    assert survived == 1                                       # FK row survived -> loss-free

    # Idempotent: a second layered sync changes nothing.
    again = svc.sync(ObsidianAdapter(vault, root_label="Obsidian"))
    assert again.deleted == 0 and kb.get_by_path("Obsidian.TeamHub.Foo").id == foo.id


def test_sync_with_stamp_backfills_frontmatter_id_losslessly(tmp_path, kb, pool):
    # An existing unstamped note, ingested under its path identity, then re-synced with
    # stamping on: the file gains a deterministic frontmatter id AND its node is re-keyed in
    # place so a referencing row (here entity_candidates) survives.
    from kb_core.adapters.obsidian.stamp import current_id, mint_id
    vault = _vault(tmp_path, {"TeamHub/Foo.md": "# Foo\nbody"})
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(vault))
    foo = kb.get_by_path("TeamHub.Foo")
    assert foo.external_id == "TeamHub/Foo.md"                  # path identity, no id yet
    with pool.connection() as c:
        c.execute("INSERT INTO entity_candidates (memory_id, entity_type, normalized_name, raw_name) "
                  "VALUES (%s, 'topic', 'foo', 'Foo')", (foo.id,))

    svc.sync(ObsidianAdapter(vault, stamp=True))

    stamped = mint_id("TeamHub/Foo.md")
    assert current_id((vault / "TeamHub" / "Foo.md").read_text()) == stamped   # written to the file
    moved = kb.get_by_path("TeamHub.Foo")
    assert moved.external_id == stamped and moved.id != foo.id   # re-keyed to the stamped id
    assert kb.get(foo.id) is None                               # old identity retired
    with pool.connection() as c:
        n = c.execute("SELECT count(*) n FROM entity_candidates WHERE memory_id=%s",
                      (moved.id,)).fetchone()["n"]
    assert n == 1                                               # reference survived the re-key

    # Idempotent: a new stamped sync neither rewrites the file nor re-keys again.
    snapshot = (vault / "TeamHub" / "Foo.md").read_text()
    svc.sync(ObsidianAdapter(vault, stamp=True))
    assert (vault / "TeamHub" / "Foo.md").read_text() == snapshot
    assert kb.get_by_path("TeamHub.Foo").id == moved.id


def test_sync_with_stamp_births_new_notes_already_identified(tmp_path, kb, pool):
    # A brand-new note is stamped before its first ingest, so it's born with the stable id —
    # no path-identity phase, nothing to re-key.
    from kb_core.adapters.obsidian.stamp import current_id, mint_id
    vault = _vault(tmp_path, {"new.md": "# New\nx"})
    SyncService(kb, pool).sync(ObsidianAdapter(vault, stamp=True))
    assert current_id((vault / "new.md").read_text()) == mint_id("new.md")
    assert kb.get_by_path("new").external_id == mint_id("new.md")


def test_sync_with_stamp_survives_a_rewrite_that_strips_the_id(tmp_path, kb, pool):
    """A past delete/recreate churn. A mirror rewrite dropped a note's stamped `id:`
    after prepare() had passed it and before fetch() read it; the note was read under its
    path identity, collided with its own node (PathConflict), and the stamped node — unseen
    that tick — was pruned. The next tick re-stamped the identical id and recreated the node,
    losing its enrichment, embedding, edges and access count. An unstamped read under stamping
    must resolve to the id the stamper would write, so the same node updates in place."""
    from kb_core.adapters.obsidian.stamp import current_id, mint_id
    vault = _vault(tmp_path, {"Projects/plan.md": "# Plan\nbody"})
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(vault, stamp=True))
    node = kb.get_by_path("Projects.plan")
    assert node.external_id == mint_id("Projects/plan.md")

    # The race: the file is rewritten without its id and read before any stamp pass —
    # modelled by an adapter whose prepare() does nothing this tick.
    (vault / "Projects" / "plan.md").write_text("# Plan\nbody v2", encoding="utf-8")
    racing = ObsidianAdapter(vault, stamp=True)
    racing.prepare = lambda kb: None
    res = svc.sync(racing)

    assert res.errors == []                                      # no PathConflict
    assert res.deleted == 0                                      # nothing pruned
    same = kb.get_by_path("Projects.plan")
    assert same.id == node.id and same.content == "# Plan\nbody v2"   # updated in place
    events = [r["event"] for r in kb.ingest_log(limit=20)]
    assert "node_deleted" not in events and "ingest_failed" not in events

    # The next ordinary tick restores the stamp line without touching identity.
    svc.sync(ObsidianAdapter(vault, stamp=True))
    assert current_id((vault / "Projects" / "plan.md").read_text()) == mint_id("Projects/plan.md")
    assert kb.get_by_path("Projects.plan").id == node.id


# --- stuck records: one ingest_failed per change, resolved when the record leaves ---

def _bad_record(eid="bad"):
    # An unregistered type fails validate() inside kb.upsert — the engine refuses it.
    return NodeRecord(source="stub", external_id=eid, type="bogus",
                      content="x", slug="bad_slug")


def test_sync_logs_one_ingest_failed_per_stuck_record(kb, pool):
    svc = SyncService(kb, pool)
    r1 = svc.sync(_StubAdapter([_bad_record()]), prune=False)
    r2 = svc.sync(_StubAdapter([_bad_record()]), prune=False)
    assert len(r1.errors) == 1 and len(r2.errors) == 1     # daemon log lines unchanged
    failed = kb.ingest_log(event="ingest_failed")
    assert len(failed) == 1                                  # not one per tick
    assert failed[0]["source"] == "stub"
    assert failed[0]["external_id"] == "bad"
    assert failed[0]["path"] == "bad_slug"                   # the intended path
    assert "unknown type: bogus" in failed[0]["detail"]["error"]
    assert [s["external_id"] for s in kb.stuck_ingest_records()] == ["bad"]


def test_sync_logs_again_when_the_error_text_changes(kb, pool):
    svc = SyncService(kb, pool)
    svc.sync(_StubAdapter([_bad_record()]), prune=False)
    empty = NodeRecord(source="stub", external_id="bad", type="note", content="x", slug="!!!")
    svc.sync(_StubAdapter([empty]), prune=False)             # a different failure
    errors = [r["detail"]["error"] for r in kb.ingest_log(event="ingest_failed")]
    assert len(errors) == 2 and errors[0] != errors[1]
    assert kb.stuck_ingest_records()[0]["error"] == errors[0]


def test_sync_resolves_a_stuck_record_that_left_the_source(kb, pool):
    svc = SyncService(kb, pool)
    svc.sync(_StubAdapter([_bad_record()]), prune=True)
    svc.sync(_StubAdapter([]), prune=True)
    resolved = kb.ingest_log(event="ingest_resolved")
    assert len(resolved) == 1
    assert resolved[0]["external_id"] == "bad"
    assert resolved[0]["path"] == "bad_slug"
    assert resolved[0]["detail"] == {"reason": "gone from source"}
    assert kb.stuck_ingest_records() == []


def test_sync_no_prune_does_not_resolve_a_record_that_left_the_source(kb, pool):
    # --no-prune means "absence from this fetch is not trusted" -- the resolve pass must
    # respect that the same way the delete pass does.
    svc = SyncService(kb, pool)
    svc.sync(_StubAdapter([_bad_record()]), prune=False)
    svc.sync(_StubAdapter([]), prune=False)
    assert kb.ingest_log(event="ingest_resolved") == []
    assert [s["external_id"] for s in kb.stuck_ingest_records()] == ["bad"]


def test_sync_landed_record_leaves_the_stuck_set_without_a_resolved_row(kb, pool):
    svc = SyncService(kb, pool)
    svc.sync(_StubAdapter([_bad_record()]), prune=False)
    svc.sync(_StubAdapter([_stub_record(eid="bad", slug="bad_slug")]), prune=False)
    assert kb.get_by_path("bad_slug") is not None
    assert kb.stuck_ingest_records() == []
    assert kb.ingest_log(event="ingest_resolved") == []      # node_created supersedes


def test_sync_records_no_path_when_the_slug_is_unsanitizable(kb, pool):
    rec = NodeRecord(source="stub", external_id="weird", type="note", content="x", slug="!!!")
    SyncService(kb, pool).sync(_StubAdapter([rec]), prune=False)
    row = kb.ingest_log(event="ingest_failed")[0]
    assert row["external_id"] == "weird"
    assert row["path"] is None


def test_sync_dry_run_touches_no_failure_state(kb, pool):
    svc = SyncService(kb, pool)
    svc.sync(_StubAdapter([_bad_record()]), prune=False, dry_run=True)
    assert kb.ingest_log(limit=10) == []                     # nothing logged on a dry run
    svc.sync(_StubAdapter([_bad_record()]), prune=False)     # now really stuck
    svc.sync(_StubAdapter([]), prune=False, dry_run=True)    # dry run must not resolve it
    assert kb.ingest_log(event="ingest_resolved") == []
    assert [s["external_id"] for s in kb.stuck_ingest_records()] == ["bad"]


def test_sync_resolves_via_landed_on_a_no_op_recovery_upsert(kb, pool):
    # A PathConflict's natural recovery (move the file back) upserts UNCHANGED content, so
    # _persist_node's no-op gate writes no node event -- without an explicit 'landed'
    # resolve, the record would stay stuck forever despite having recovered.
    svc = SyncService(kb, pool)
    svc.sync(_StubAdapter([_stub_record(eid="x", slug="x")]), prune=False)   # lands
    svc.sync(_StubAdapter([_bad_record(eid="x")]), prune=False)              # fails -> stuck
    assert [s["external_id"] for s in kb.stuck_ingest_records()] == ["x"]
    svc.sync(_StubAdapter([_stub_record(eid="x", slug="x")]), prune=False)   # same content
    assert kb.stuck_ingest_records() == []
    resolved = kb.ingest_log(event="ingest_resolved")
    assert len(resolved) == 1
    assert resolved[0]["external_id"] == "x"
    assert resolved[0]["detail"] == {"reason": "landed"}


def test_sync_log_ingest_failed_raising_does_not_abort_the_tick(kb, pool, monkeypatch):
    # Observability must never abort ingestion: a pool blip in log_ingest_failed must not
    # cost the rest of the batch its upserts/prune/sync_state.
    def boom(*args, **kwargs):
        raise RuntimeError("db down")
    monkeypatch.setattr(kb, "log_ingest_failed", boom)
    svc = SyncService(kb, pool)
    records = [_bad_record(), _stub_record(eid="good", slug="good")]
    res = svc.sync(_StubAdapter(records), prune=False)
    assert any("unknown type: bogus" in e for e in res.errors)     # original error line kept
    assert any("log failed: db down" in e for e in res.errors)     # observability failure recorded
    assert kb.get_by_path("good") is not None                      # good record still landed


# --- changed vs. seen: an idle re-sync writes nothing and says so -------------------

def test_resync_of_unchanged_vault_reports_zero_changed(tmp_path, kb, pool):
    vault = _vault(tmp_path, {"a.md": "aaa", "b.md": "bbb"})
    svc = SyncService(kb, pool)
    first = svc.sync(ObsidianAdapter(vault))
    assert (first.upserted, first.changed) == (2, 2)          # both created
    idle = svc.sync(ObsidianAdapter(vault))
    assert (idle.upserted, idle.changed) == (2, 0)            # seen, nothing written
    (vault / "b.md").write_text("bbb edited", encoding="utf-8")
    edited = svc.sync(ObsidianAdapter(vault))
    assert (edited.upserted, edited.changed) == (2, 1)        # only the edited note


def test_changed_counts_writes_through_a_custom_ingestor(kb, pool):
    adapter = _StubAdapter([_stub_record()], ingestor=_RecordingIngestor())
    svc = SyncService(kb, pool)
    assert svc.sync(adapter, prune=False).changed == 1
    assert svc.sync(adapter, prune=False).changed == 0


def test_dry_run_reports_no_changes(tmp_path, kb, pool):
    vault = _vault(tmp_path, {"a.md": "aaa"})
    res = SyncService(kb, pool).sync(ObsidianAdapter(vault), dry_run=True)
    assert (res.upserted, res.changed) == (1, 0)


def test_node_writes_counts_only_real_writes(kb):
    rec = _stub_record()
    n0 = kb.node_writes()
    kb.upsert(rec)
    assert kb.node_writes() == n0 + 1
    kb.upsert(rec)                                            # no-op gate: no write
    assert kb.node_writes() == n0 + 1
    kb.upsert(NodeRecord(source="stub", external_id="1", type="note",
                         content="hi", slug="hi", tags=["t"]))   # tags-only change
    assert kb.node_writes() == n0 + 2


def test_write_counter_without_node_writes_counts_every_upsert():
    from kb_core.adapters.base import write_counter
    read = write_counter(object())
    assert read() != read()
