"""End-to-end consolidation over a seeded KB with a fake inference client."""
import json


from kb_core.consolidate import run_once
from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS


class FakeClient:
    """Labels clusters by which seeded entity shows up; embeds everything as a unit vec."""
    def __init__(self, coherent=True):
        self.coherent = coherent
        self.completions = 0

    def complete(self, system_prompt, user_prompt, *, schema=None):
        self.completions += 1
        name = "Alpha project" if "AlphaRepo" in user_prompt else "Beta project"
        return json.dumps({"coherent": self.coherent, "name": name,
                           "kind": "project", "description": "seeded cluster"})

    def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]


class LabelOnlyClient(FakeClient):
    def embed(self, texts):
        raise AssertionError("the concept label client must not embed")


class EmbeddingOnlyClient:
    def __init__(self):
        self.texts = []

    def complete(self, system_prompt, user_prompt, *, schema=None):
        raise AssertionError("the concept embedding client must not label")

    def embed(self, texts):
        self.texts.extend(texts)
        return [[1.0] + [0.0] * 1023 for _ in texts]


def _vec(axis):
    v = [0.0] * 1024
    v[axis] = 1.0
    return v


def _seed_two_projects(kb):
    """Two 3-memory clusters, each glued by 2 entities; embeddings reinforce the split.
    Returns every seeded memory id, so a caller can hang a cross-cutting hub off them."""
    memory_ids = []
    for prefix, ents, axis in (("a", ("AlphaRepo", "AlphaPerson"), 0),
                               ("b", ("BetaRepo", "BetaPerson"), 1)):
        entities = [kb.upsert_entity(e, categories=["tool"]) for e in ents]
        for i in range(3):
            m = kb.upsert(NodeRecord(source="agent", external_id=f"{prefix}{i}",
                                     type="note", content=f"note about {ents[0]}",
                                     slug=f"{prefix}{i}", title=f"{prefix}{i}"))
            kb.set_embedding(m.id, _vec(axis), embedded_hash=f"h{prefix}{i}")
            for ent in entities:
                kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)
            memory_ids.append(m.id)
    return memory_ids


def test_run_once_discovers_and_persists_two_concepts(kb):
    _seed_two_projects(kb)
    r = run_once(kb, FakeClient(), min_memories=3)
    assert r["kept"] == 2 and r["created"] == 2 and r["discarded"] == 0
    active = kb.active_concepts()
    assert {c["name"] for c in active} == {"Alpha project", "Beta project"}
    for c in active:
        assert len(c["memory_ids"]) == 3                   # entities counted separately
        assert c["member_count"] == 5                      # 3 memories + 2 entities


def test_run_once_keeps_hub_capped_entity_on_concept_rosters(kb):
    """A hub entity is barred from CLUSTERING but must still land on the rosters of the
    concepts its memories formed — the partition is untouched, the membership is not."""
    memory_ids = _seed_two_projects(kb)
    hub = kb.upsert_entity("Avery", categories=["person"])
    for mid in memory_ids:                       # in all 6 memories -> over the cap
        kb.link_entity(mid, hub.node.id, MENTIONS, weight=0.9)

    r = run_once(kb, FakeClient(), min_memories=3)

    assert r["hub_dropped"] == 1                 # kept out of the graph
    assert r["kept"] == 2                        # ... so the partition is unchanged
    active = kb.active_concepts()
    assert {c["name"] for c in active} == {"Alpha project", "Beta project"}
    for c in active:
        assert len(c["memory_ids"]) == 3         # 3 memories, as without the hub
        assert c["member_count"] == 6            # 3 memories + 2 entities + the hub


def test_run_once_coherence_gate_cuts_a_weld_frequency_would_keep(kb):
    """The weld case: an entity spanning both projects is a pure weld — its memories
    are LESS alike than two drawn at random (2 of 3 pairs orthogonal -> 0.33 against a
    0.40 baseline). Frequency cannot see it (support 3, cap 4); coherence cuts it. Three
    mentions, not two, because `min_support` refuses to condemn a single-sample estimate.
    And per the attach/cluster split it still lands on both concepts' rosters, so cutting
    it costs no visibility."""
    memory_ids = _seed_two_projects(kb)
    weld = kb.upsert_entity("Avery", categories=["person"])
    for mid in (memory_ids[0], memory_ids[1], memory_ids[3]):           # 2 a-, 1 b-memory
        kb.link_entity(mid, weld.node.id, MENTIONS, weight=0.9)

    frequency = run_once(kb, FakeClient(), min_memories=3, apply=False)
    assert frequency["hub_dropped"] == 0            # support 2 is far under the cap

    r = run_once(kb, FakeClient(), min_memories=3, min_coherence_lift=1.0)

    assert r["hub_dropped"] == 1                    # coherence sees what frequency cannot
    assert r["kept"] == 2                           # the weld did not merge the projects
    for c in kb.active_concepts():
        assert len(c["memory_ids"]) == 3
        assert c["member_count"] == 6               # 3 memories + 2 entities + the weld


def test_run_once_separates_concept_labeling_from_embedding(kb):
    _seed_two_projects(kb)
    label_client = LabelOnlyClient()
    embedding_client = EmbeddingOnlyClient()

    r = run_once(kb, label_client, embedding_client=embedding_client, min_memories=3)

    assert r["kept"] == 2
    assert label_client.completions == 2
    assert sorted(embedding_client.texts) == [
        "Alpha project\nseeded cluster", "Beta project\nseeded cluster"]


def test_consolidation_clients_resolve_via_inference_toml(routing_toml):
    from kb_core.consolidate import build_consolidation_clients
    from kb_core.ingest.claude_cli_client import ClaudeCliInferenceClient
    from kb_core.ingest.inference import HTTPInferenceClient

    label_client, embedding_client = build_consolidation_clients(
        {"KB_INFERENCE_ROUTING": routing_toml})

    assert isinstance(label_client, ClaudeCliInferenceClient)
    assert label_client.model == "claude-sonnet-5"
    assert isinstance(embedding_client, HTTPInferenceClient)


def test_retired_concept_label_env_warns(routing_toml, capsys):
    from kb_core.consolidate import build_consolidation_clients

    build_consolidation_clients({"KB_INFERENCE_ROUTING": routing_toml,
                                 "KB_CONCEPT_LABEL_PROVIDER": "anthropic"})
    assert "KB_CONCEPT_LABEL_PROVIDER is set but no longer read" in capsys.readouterr().out


def test_second_run_carries_concept_ids(kb):
    _seed_two_projects(kb)
    run_once(kb, FakeClient(), min_memories=3)
    before = {c["name"]: c["id"] for c in kb.active_concepts()}
    r2 = run_once(kb, FakeClient(), min_memories=3)
    after = {c["name"]: c["id"] for c in kb.active_concepts()}
    assert r2["updated"] == 2 and r2["created"] == 0
    assert before == after                                 # stable ids across runs


def test_vanished_cluster_retires_its_concept(kb, pool):
    _seed_two_projects(kb)
    run_once(kb, FakeClient(), min_memories=3)
    with pool.connection() as conn:                        # kill cluster B's memories
        conn.execute("UPDATE nodes SET dead_at = now() WHERE slug LIKE 'b%'")
    r = run_once(kb, FakeClient(), min_memories=3)
    assert r["retired"] == 1
    assert {c["name"] for c in kb.active_concepts()} == {"Alpha project"}


def test_incoherent_communities_are_discarded_not_persisted(kb):
    _seed_two_projects(kb)
    r = run_once(kb, FakeClient(coherent=False), min_memories=3)
    assert r["discarded"] == 2 and r["kept"] == 0
    assert kb.active_concepts() == []


def test_preview_mode_writes_nothing(kb, pool):
    _seed_two_projects(kb)
    r = run_once(kb, FakeClient(), min_memories=3, apply=False)
    assert len(r["preview"]) == 2 and "run_id" not in r
    with pool.connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM concept_runs").fetchone()["n"]
    assert n == 0 and kb.active_concepts() == []


def test_no_client_falls_back_to_placeholder_labels(kb):
    _seed_two_projects(kb)
    r = run_once(kb, None, min_memories=3)
    assert r["kept"] == 2
    names = {c["name"] for c in kb.active_concepts()}
    assert any("AlphaRepo" in n for n in names)            # named from top entities


class _FailingClient:
    def complete(self, system_prompt, user_prompt, *, schema=None):
        raise RuntimeError("inference down")

    def embed(self, texts):
        raise RuntimeError("inference down")


def test_failed_label_keeps_carried_concept_label():
    from kb_core.consolidate import _label

    prev = {"name": "Alpha project", "kind": "project", "description": "seeded cluster"}
    lab = _label(_FailingClient(), {"entities": [("AlphaRepo", "repo")], "memories": []},
                 prev)
    assert lab == {"coherent": True, "name": "Alpha project", "kind": "project",
                   "description": "seeded cluster"}          # outage never downgrades


def test_placeholder_previous_name_never_anchors():
    from kb_core.consolidate import _label

    class _Capture:
        user = None

        def complete(self, system_prompt, user_prompt, *, schema=None):
            self.user = user_prompt
            return json.dumps({"coherent": True, "name": "Real name",
                               "kind": "project", "description": "d"})

    cap = _Capture()
    lab = _label(cap, {"entities": [("A", None)], "memories": []},
                 {"name": "A / B / C", "kind": "other", "description": ""})
    assert "previous name" not in cap.user                   # hint suppressed
    assert lab["name"] == "Real name"


def test_failed_label_with_placeholder_prev_gets_fresh_placeholder():
    from kb_core.consolidate import _label

    lab = _label(_FailingClient(), {"entities": [("Zeta", None)], "memories": []},
                 {"name": "A / B / C", "kind": "other", "description": ""})
    assert lab["name"] == "Zeta"                             # stale placeholder not kept


def test_dismissed_concept_communities_are_skipped(kb):
    _seed_two_projects(kb)
    run_once(kb, FakeClient(), min_memories=3)
    alpha = next(c for c in kb.active_concepts() if "Alpha" in c["name"])
    kb.set_concept_status(alpha["id"], "dismissed")
    r = run_once(kb, FakeClient(), min_memories=3)
    assert r["dismissed_hits"] == 1
    names = {c["name"] for c in kb.active_concepts()}
    assert not any("Alpha" in n for n in names)          # stays gone
    assert any("Beta" in n for n in names)


def test_locked_label_survives_runs_without_llm_call(kb):
    _seed_two_projects(kb)
    run_once(kb, FakeClient(), min_memories=3)
    alpha = next(c for c in kb.active_concepts() if "Alpha" in c["name"])
    kb.set_concept_label(alpha["id"], name="My Startup", kind="project")
    client = FakeClient()
    run_once(kb, client, min_memories=3)
    fresh = next(c for c in kb.active_concepts() if c["id"] == alpha["id"])
    assert fresh["name"] == "My Startup"                 # lock held
    assert client.completions == 1                       # only Beta was labeled


def test_member_remove_override_survives_reapply(kb):
    _seed_two_projects(kb)
    run_once(kb, FakeClient(), min_memories=3)
    alpha = next(c for c in kb.active_concepts() if "Alpha" in c["name"])
    victim = str(alpha["memory_ids"][0])
    kb.override_concept_member(alpha["id"], victim, "remove")
    run_once(kb, FakeClient(), min_memories=3)           # rebuild would re-add it
    fresh = next(c for c in kb.active_concepts() if c["id"] == alpha["id"])
    assert victim not in {str(i) for i in fresh["memory_ids"]}


def test_cannot_link_constraint_flows_through_run(kb):
    from kb_core.entities import MENTIONS
    _seed_two_projects(kb)
    # Glue the two projects with several shared hubs so they'd merge into one community.
    # A single hub's support-damped mention weight (conf=0.9 over support=6 ->
    # ~0.32, see concepts.mention_edge_weight) can't outweigh each side's intra-cluster
    # mention edges (0.45) plus embedding-kNN edges (sim 1.0).
    # kNN is disabled here (knn_min_sim=2.0, unreachable) so gluing is decided by the
    # mention graph alone, and several low-support hubs at a coarser resolution supply
    # enough combined pull to merge the two sides into one community.
    # Each SharedOrg's support (6, all memories) is exactly what the hub cap
    # now strips from the CLUSTERING graph by default — hub_share=None here so this test
    # keeps exercising the cannot_link constraint machinery it's actually about, not the
    # cap (see tests/test_concept_graph.py for that).
    for h in range(3):
        shared = kb.upsert_entity(f"SharedOrg{h}", categories=["org"])
        for slug in ("a0", "a1", "a2", "b0", "b1", "b2"):
            node = next(n for n in kb.list(limit=500) if n.slug == slug)
            kb.link_entity(node.id, shared.node.id, MENTIONS, weight=0.9)
    r1 = run_once(kb, FakeClient(), min_memories=3, knn_min_sim=2.0, resolution=0.2,
                 hub_share=None)
    assert r1["kept"] == 1                               # glued without the rule
    a_ent = next(e for e in kb.active_instances() if e["name"] == "AlphaRepo")
    b_ent = next(e for e in kb.active_instances() if e["name"] == "BetaRepo")
    kb.add_concept_constraint("cannot_link", a_ent["id"], b_ent["id"],
                              note="different projects")
    r2 = run_once(kb, FakeClient(), min_memories=3, knn_min_sim=2.0, resolution=0.2,
                 hub_share=None)
    assert r2["kept"] == 2                               # split by the rule


def test_shared_entity_becomes_member_of_both_concepts(kb, pool):
    """A person mentioned across two projects belongs to BOTH concepts — entity
    membership is derived from mention attachment, not locked to one partition side.
    Quill's support (5 of the graph's 6 memories) exceeds the hub cap's default floor,
    so hub_share=None here — this test is about attach_entities' cross-community
    derivation, not the hub cap (see tests/test_concept_graph.py for that)."""
    from kb_core.entities import MENTIONS
    _seed_two_projects(kb)
    quill = kb.upsert_entity("Quill", categories=["person"])
    for slug in ("a0", "a1", "b0", "b1", "b2"):
        node = next(n for n in kb.list(limit=500) if n.slug == slug)
        kb.link_entity(node.id, quill.node.id, MENTIONS, weight=0.9)
    r = run_once(kb, FakeClient(), min_memories=3, hub_share=None)
    assert r["kept"] == 2
    with pool.connection() as conn:
        n = conn.execute(
            "SELECT count(DISTINCT concept_id) AS n FROM concept_members "
            "WHERE node_id = %s", (quill.node.id,)).fetchone()["n"]
    assert n == 2


def test_separation_rule_keeps_concepts_apart_through_run(kb):
    from kb_core.entities import MENTIONS
    _seed_two_projects(kb)
    r1 = run_once(kb, FakeClient(), min_memories=3)
    assert r1["kept"] == 2                               # two concepts form
    alpha = next(c for c in kb.active_concepts() if "Alpha" in c["name"])
    beta = next(c for c in kb.active_concepts() if "Beta" in c["name"])
    kb.add_concept_separation(alpha["id"], beta["id"], note="unrelated projects")
    # Same gluing as the cannot_link flow test: without the rule these would merge.
    # Each SharedOrg's support (6, all memories) exceeds the hub cap's default floor (4),
    # so hub_share=None here — otherwise the glue is dropped from the graph before the
    # separation rule is ever exercised (see tests/test_concept_graph.py for the cap itself).
    for h in range(3):
        shared = kb.upsert_entity(f"SharedOrg{h}", categories=["org"])
        for slug in ("a0", "a1", "a2", "b0", "b1", "b2"):
            node = next(n for n in kb.list(limit=500) if n.slug == slug)
            kb.link_entity(node.id, shared.node.id, MENTIONS, weight=0.9)
    r2 = run_once(kb, FakeClient(), min_memories=3, knn_min_sim=2.0, resolution=0.2,
                 hub_share=None)
    assert r2["kept"] == 2                               # kept apart by the rule
    names = {c["name"] for c in kb.active_concepts()}
    assert names == {"Alpha project", "Beta project"}    # both ids carried


def test_member_pin_override_applies_and_survives_reapply(kb):
    _seed_two_projects(kb)
    run_once(kb, FakeClient(), min_memories=3)
    alpha = next(c for c in kb.active_concepts() if "Alpha" in c["name"])
    beta = next(c for c in kb.active_concepts() if "Beta" in c["name"])
    outsider = str(beta["memory_ids"][0])            # a memory from the other concept
    kb.override_concept_member(alpha["id"], outsider, "pin")
    fresh = next(c for c in kb.active_concepts() if c["id"] == alpha["id"])
    assert outsider in {str(i) for i in fresh["memory_ids"]}   # applied immediately
    run_once(kb, FakeClient(), min_memories=3)                 # rebuild would drop it
    again = next(c for c in kb.active_concepts() if c["id"] == alpha["id"])
    assert outsider in {str(i) for i in again["memory_ids"]}   # reapplied by post-pass


def test_member_pin_resolves_entity_kind(kb, pool):
    _seed_two_projects(kb)
    run_once(kb, FakeClient(), min_memories=3)
    alpha = next(c for c in kb.active_concepts() if "Alpha" in c["name"])
    ent = next(e for e in kb.active_instances() if e["name"] == "BetaRepo")
    kb.override_concept_member(alpha["id"], ent["id"], "pin")
    run_once(kb, FakeClient(), min_memories=3)
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT member_kind FROM concept_members WHERE concept_id = %s "
            "AND node_id = %s", (alpha["id"], ent["id"])).fetchone()
    assert row["member_kind"] == "entity"                      # kind resolved via entities join


def test_run_once_records_knn_edge_build_time(kb):
    """The O(n^2) kNN cost is measured per run so kb-healthcheck can watch it.

    concept_runs' own duration cannot serve: begin_concept_run fires after the graph is
    built, so that column times only apply_concept_run.
    """
    _seed_two_projects(kb)
    r = run_once(kb, FakeClient(), min_memories=3)
    assert isinstance(r["knn_ms"], float)
    assert r["knn_ms"] >= 0.0
    with kb._pool.connection() as c:
        row = c.execute("SELECT jsonb_typeof(stats->'knn_ms') AS t FROM concept_runs "
                        "WHERE id = %s", (r["run_id"],)).fetchone()
    # Not just "is not null": healthcheck's reader requires jsonb_typeof(...) = 'number'
    # (see healthcheck.py's _db_signals) -- a string value would pass a weaker assertion
    # here while silencing the capacity alarm permanently.
    assert row["t"] == "number"


def _last_params(pool):
    """run_once returns stats, not params -- params is persisted on the concept_runs row."""
    with pool.connection() as conn:
        return conn.execute(
            "select params from concept_runs order by id desc limit 1").fetchone()["params"]


def test_run_once_records_entity_alpha_in_params(kb, pool):
    """Every run must be self-describing: concept_runs.params is the only record of what
    configuration produced a given partition."""
    _seed_two_projects(kb)
    run_once(kb, FakeClient(), min_memories=3, entity_alpha=2.0)
    assert _last_params(pool)["entity_alpha"] == 2.0


def test_run_once_entity_alpha_defaults_to_one(kb, pool):
    _seed_two_projects(kb)
    run_once(kb, FakeClient(), min_memories=3)
    assert _last_params(pool)["entity_alpha"] == 1.0


def test_run_once_entity_alpha_default_does_not_change_the_outcome(kb):
    """The knob must be a no-op at its default -- same concepts as the existing
    test_run_once_discovers_and_persists_two_concepts."""
    _seed_two_projects(kb)
    r = run_once(kb, FakeClient(), min_memories=3, entity_alpha=1.0)
    assert r["kept"] == 2 and r["created"] == 2 and r["discarded"] == 0


class RunRowProbingClient(FakeClient):
    """Asserts, from inside the labeler, that the run row is already open and marked
    'labeling'. This is the whole point of opening the row before the LLM work: until
    now begin_concept_run fired AFTER labeling, so every row recorded 0s and an
    in-flight pass was invisible."""
    def __init__(self, kb):
        super().__init__()
        self.kb = kb
        self.seen = []

    def complete(self, system_prompt, user_prompt, *, schema=None):
        with self.kb._pool.connection() as conn:
            self.seen.append(conn.execute(
                "SELECT status, phase, trigger_reason FROM concept_runs"
            ).fetchall())
        return super().complete(system_prompt, user_prompt, schema=schema)


def test_run_row_is_open_and_labeling_while_the_labeler_runs(kb):
    _seed_two_projects(kb)
    client = RunRowProbingClient(kb)
    run_once(kb, client, min_memories=3, trigger_reason="manual")
    assert client.seen, "the labeler never ran"
    for snapshot in client.seen:
        assert snapshot == [{"status": "running", "phase": "labeling",
                            "trigger_reason": "manual"}]


def test_finished_run_records_done_and_the_applying_phase(kb):
    _seed_two_projects(kb)
    r = run_once(kb, FakeClient(), min_memories=3)
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT status, phase, trigger_reason, finished_at IS NOT NULL AS done "
            "FROM concept_runs WHERE id = %s", (r["run_id"],)).fetchone()
    assert row == {"status": "done", "phase": "applying",
                   "trigger_reason": "churn", "done": True}


def test_a_crash_while_applying_marks_the_run_failed_not_running(kb, monkeypatch):
    """A pass that dies after the row is open must not leave 'running' behind — that
    would pin the dashboard on "Consolidating…" forever. Its stored stats must also
    carry the partial progress plus the failure reason -- a failed row that stores
    nothing is undiagnosable, and (unlike a successful run) there is no returned dict
    for a caller to read the reason from instead.

    The failure is injected at `apply_concept_run` rather than in the labeler on purpose:
    `_label` and `_embed_labels` swallow every exception by design (a bad reply must
    degrade names, never structure) -- except a labeler ProviderUnavailable, which aborts
    the pass (see test_a_provider_outage_mid_labelling_pauses_and_persists_no_concepts)."""
    import pytest

    _seed_two_projects(kb)
    monkeypatch.setattr(type(kb), "apply_concept_run",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("apply boom")))
    with pytest.raises(RuntimeError, match="apply boom"):
        run_once(kb, FakeClient(), min_memories=3)
    with kb._pool.connection() as conn:
        rows = conn.execute("SELECT status, stats FROM concept_runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    stats = rows[0]["stats"]
    assert "apply boom" in stats["error"]
    assert "communities" in stats or "kept" in stats   # partial progress survived


def test_a_crash_before_any_stats_exist_still_marks_the_run_failed(kb, monkeypatch):
    """The new ordering opens a 'running' row before the graph is even built, so a crash
    in that early phase -- impossible to observe under the old code, where
    begin_concept_run fired last -- must still resolve to 'failed' with the reason
    stored, even though `_pass` never got far enough to build a `stats` dict at all."""
    import pytest

    _seed_two_projects(kb)
    monkeypatch.setattr(type(kb), "concept_mention_edges",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph boom")))
    with pytest.raises(RuntimeError, match="graph boom"):
        run_once(kb, FakeClient(), min_memories=3)
    with kb._pool.connection() as conn:
        rows = conn.execute("SELECT status, stats FROM concept_runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "graph boom" in rows[0]["stats"]["error"]


def test_preview_opens_no_run_row(kb):
    _seed_two_projects(kb)
    r = run_once(kb, FakeClient(), min_memories=3, apply=False)
    assert "run_id" not in r
    with kb._pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM concept_runs").fetchone()["n"] == 0


def test_stored_stats_carry_the_label_error_for_diagnosis(kb):
    """label failures are fail-open, so without the reason on the row a degraded layer
    is undiagnosable after the fact."""
    class HalfBroken(FakeClient):
        def complete(self, system_prompt, user_prompt, *, schema=None):
            raise RuntimeError("claude-cli: not logged in")

    _seed_two_projects(kb)
    r = run_once(kb, HalfBroken(), min_memories=3)
    with kb._pool.connection() as conn:
        stats = conn.execute("SELECT stats FROM concept_runs WHERE id = %s",
                             (r["run_id"],)).fetchone()["stats"]
    assert "not logged in" in stats["label_error"]
    assert stats["label_failures"] >= 1


def test_preview_needs_no_lock(kb):
    """A preview writes nothing, so it must not contend with a running pass — otherwise
    `kb-consolidate` (which previews by default) would refuse to show you anything
    while the daemon happened to be working."""
    _seed_two_projects(kb)
    with kb.consolidation_lock():
        r = run_once(kb, FakeClient(), min_memories=3, apply=False)
    assert r["kept"] == 2 and "run_id" not in r


def test_broken_prompts_overlay_aborts_the_whole_pass_before_any_write(kb, tmp_path):
    """A PolicyError from a broken prompts overlay (missing include, missing file) is a
    configuration error, not an inference error: it must abort the WHOLE pass before any
    DB write (no audit row, no advisory lock held), never be swallowed per-community by
    `_label`'s fail-open try/except. `run_once` resolves the labeler's system prompt
    exactly once, at the top, before `_pass` touches the graph or the DB -- so the
    client here must never even be reached, and (unlike a mid-pass crash, which still
    stores a 'failed' row) NO concept_runs row may exist afterwards. A community must
    actually exist so the labeling path is reached at all -- with none, the old
    per-community `kb.prompt` call would never fire either, and this test would pass
    for the wrong reason."""
    import pytest

    from kb_core.knowledge_base import KnowledgeBase
    from kb_core.policy import Policy, PolicyError, Prompts
    from kb_core.registry import default_registry

    class UnusedClient:
        def complete(self, *a, **k):
            raise AssertionError("client must not be reached when the overlay is broken")

        def embed(self, *a, **k):
            raise AssertionError("client must not be reached when the overlay is broken")

    _seed_two_projects(kb)
    (tmp_path / "concept_judge.system.md").write_text("{{include:missing.md}}")
    broken = KnowledgeBase(kb._pool, default_registry(),
                           policy=Policy(prompts=Prompts(str(tmp_path))))
    with pytest.raises(PolicyError):
        run_once(broken, UnusedClient(), min_memories=3)
    with kb._pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM concept_runs").fetchone()["n"] == 0


def test_a_provider_outage_mid_labelling_pauses_and_persists_no_concepts(kb):
    """An outage is not a bad label: the pass aborts on the first ProviderUnavailable
    instead of waiting out every community's timeout and storing fallback names. The
    provider is paused, the run row is marked failed, and no concept is written."""
    from datetime import datetime, timedelta, timezone

    import pytest

    from kb_core.ingest.inference import ProviderUnavailable

    class DiesOnSecondLabel(FakeClient):
        def complete(self, system_prompt, user_prompt, *, schema=None):
            if self.completions >= 1:
                self.completions += 1
                raise ProviderUnavailable(
                    "no agent answered", provider="agent",
                    until=datetime.now(timezone.utc) + timedelta(minutes=10))
            return super().complete(system_prompt, user_prompt, schema=schema)

    _seed_two_projects(kb)
    client = DiesOnSecondLabel()
    with pytest.raises(ProviderUnavailable):
        run_once(kb, client, min_memories=3)
    assert client.completions == 2
    assert kb.provider_paused("agent") is not None
    assert kb.active_concepts() == []
    with kb._pool.connection() as conn:
        rows = conn.execute("SELECT status, stats FROM concept_runs").fetchall()
    assert [r["status"] for r in rows] == ["failed"]
    assert "no agent answered" in rows[0]["stats"]["error"]


def test_a_preview_pass_also_pauses_on_a_provider_outage(kb):
    import pytest

    from kb_core.ingest.inference import ProviderUnavailable

    class Offline(FakeClient):
        def complete(self, system_prompt, user_prompt, *, schema=None):
            raise ProviderUnavailable("claude-cli: session limit", provider="claude-cli")

    _seed_two_projects(kb)
    with pytest.raises(ProviderUnavailable):
        run_once(kb, Offline(), min_memories=3, apply=False)
    assert kb.provider_paused("claude-cli") is not None        # default re-probe window
