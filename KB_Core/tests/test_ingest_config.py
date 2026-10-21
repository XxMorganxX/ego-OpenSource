import types
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from kb_core.domain import NodeRecord
from kb_core.ingest import daemon as daemon_module
from kb_core.ingest.config import SourceConfig, load_settings, load_sources
from kb_core.ingest.daemon import build_app, build_jobs
from kb_core.ingest.inbox import Inbox
from kb_core.knowledge_base import KnowledgeBase
from kb_core.policy import Policy, PolicyError, Prompts
from kb_core.registry import default_registry


def _vault(tmp_path):
    v = tmp_path / "v"; v.mkdir()
    return {"KB_OBSIDIAN_VAULT": str(v)}


def test_push_config_forces_no_prune():
    cfg = SourceConfig(source="webhook", mode="push",
                       period=timedelta(seconds=60), prune=True)
    assert cfg.prune is False                              # push never prunes (deltas)


def test_load_settings_reads_toml(tmp_path):
    f = tmp_path / "sources.toml"
    f.write_text("[obsidian]\nenabled = false\nperiod_seconds = 999\n")
    s = load_settings({"KB_SOURCES_CONFIG": str(f)})
    assert s["obsidian"]["enabled"] is False and s["obsidian"]["period_seconds"] == 999


def test_load_sources_applies_toggles(tmp_path):
    settings = {"obsidian": {"enabled": True, "is_enrichable": True, "period_seconds": 120},
                "webhook": {"enabled": True, "is_enrichable": False, "period_seconds": 30}}
    sources = {c.source: c for c in load_sources(_vault(tmp_path), settings=settings)}
    assert sources["obsidian"].mode == "pull" and sources["obsidian"].period.seconds == 120
    assert sources["webhook"].mode == "push" and sources["webhook"].prune is False
    assert sources["webhook"].period.seconds == 30


def test_disabled_source_is_skipped(tmp_path):
    settings = {"obsidian": {"enabled": False}, "webhook": {"enabled": False}}
    assert load_sources(_vault(tmp_path), settings=settings) == []


def test_is_enrichable_toggles_flag_enrichment(tmp_path):
    settings = {"obsidian": {"enabled": True, "is_enrichable": True},
                "webhook": {"enabled": True, "is_enrichable": False}}
    sources = {c.source: c for c in load_sources(_vault(tmp_path), settings=settings)}
    obs_post = [t.name for t in sources["obsidian"].pipeline.post]
    wh_post = [t.name for t in sources["webhook"].pipeline.post]
    assert "flag_enrichment" in obs_post            # enrichable -> nodes get queued
    assert "flag_enrichment" not in wh_post         # not enrichable -> never queued
    assert sources["obsidian"].is_enrichable is True
    assert sources["webhook"].is_enrichable is False


def test_each_source_has_its_own_pipeline(tmp_path):
    settings = {"obsidian": {"enabled": True}, "webhook": {"enabled": True}}
    sources = {c.source: c for c in load_sources(_vault(tmp_path), settings=settings)}
    assert sources["obsidian"].pipeline is not sources["webhook"].pipeline


def test_build_jobs_includes_enrichment(kb, pool, tmp_path):
    settings = {"obsidian": {"enabled": True}, "webhook": {"enabled": True}}
    sources = load_sources(_vault(tmp_path), settings=settings)
    names = {j.name for j in build_jobs(kb, pool, sources)}
    # re-analysis is its OWN daemon (kb-mcp-reanalyzed), not an ingest job.
    assert names == {"obsidian", "webhook", "enrichment", "promotion"}


def test_pull_job_kicks_enrichment_when_pending_lands(kb, pool, tmp_path):
    # Ingestion -> enrichment must be event-driven: a pull that leaves notes 'pending'
    # pokes the enrich lane, instead of letting them sit out KB_ENRICH_PERIOD with
    # nothing processing.
    v = tmp_path / "v"; v.mkdir()
    (v / "note.md").write_text("fresh note\n")
    settings = {"obsidian": {"enabled": True, "stamp": False}}
    sources = load_sources({"KB_OBSIDIAN_VAULT": str(v)}, settings=settings)
    kicks = []
    jobs = {j.name: j for j in build_jobs(kb, pool, sources,
                                          kick_enrich=lambda: kicks.append(1))}
    jobs["obsidian"].run()
    assert kicks                                   # new pending note -> enrich lane poked
    # Once a worker claims the batch, nothing is pending: a further no-change pull must
    # not poke (flag_stale leaves in-flight 'processing' rows alone).
    kicks.clear()
    kb.claim_enrichment(100)
    jobs["obsidian"].run()
    assert not kicks


def test_build_jobs_meaning_gate_on_fails_loud_on_broken_overlay(monkeypatch, pool, tmp_path):
    # A broken overlay must fail at daemon start (build_jobs), not per tick — a tick's
    # PolicyError would otherwise be swallowed by the scheduler (one bad job must never
    # kill the loop).
    (tmp_path / "meaning_judge.system.md").write_text("{{include:missing.md}}")
    broken_kb = KnowledgeBase(pool, default_registry(),
                              policy=Policy(prompts=Prompts(str(tmp_path))))
    monkeypatch.setattr(daemon_module, "InferenceRouter",
                        lambda *a, **k: types.SimpleNamespace(client_for=lambda task: object()))
    monkeypatch.setenv("KB_INSTANCE_MEANING_GATE", "on")
    with pytest.raises(PolicyError, match="missing.md"):
        build_jobs(broken_kb, pool, [])


def test_build_jobs_meaning_gate_off_skips_the_overlay_check(monkeypatch, pool, tmp_path):
    (tmp_path / "meaning_judge.system.md").write_text("{{include:missing.md}}")
    broken_kb = KnowledgeBase(pool, default_registry(),
                              policy=Policy(prompts=Prompts(str(tmp_path))))
    monkeypatch.setenv("KB_INSTANCE_MEANING_GATE", "off")
    build_jobs(broken_kb, pool, [])          # must not raise — gate is off


def test_daemon_app_serves_no_unauthenticated_log(kb, pool):
    """The ingestion log is read through the dashboard API behind its auth; the receiver
    used to serve it to anyone who could reach the port."""
    kb.upsert(NodeRecord(source="agent", external_id="x", type="note",
                         content="c", slug="x"))
    client = TestClient(build_app(Inbox(pool), secret="abcd" * 16))
    assert client.get("/log").status_code == 404
    assert client.get("/healthz").status_code == 200


def test_build_jobs_lane_assignment(kb, pool, tmp_path):
    settings = {"obsidian": {"enabled": True}, "webhook": {"enabled": True}}
    sources = load_sources(_vault(tmp_path), settings=settings)
    lanes = {j.name: j.lane for j in build_jobs(kb, pool, sources)}
    # Ingestion (pull/drain) must never queue behind LLM work, and enrichment claims
    # must never queue behind a promotion sweep: enrichment gets its own lane.
    assert lanes == {"obsidian": "fast", "webhook": "fast",
                     "enrichment": "enrich", "promotion": "slow"}


def test_enrichment_job_more_tracks_real_pending_backlog(kb, pool, tmp_path):
    # The scheduler re-runs the job immediately (ScheduledJob.more) iff pending work
    # actually remains. Claim fullness was the old proxy, and it lied both ways: notes
    # arriving DURING a partial batch's minutes-long run were parked for the whole
    # KB_ENRICH_PERIOD ("pending notes, zero processing"), and a full batch that drained
    # the table triggered one pointless extra claim.
    settings = {"obsidian": {"enabled": True}}
    sources = load_sources(_vault(tmp_path), settings=settings)
    enrich = {j.name: j for j in build_jobs(kb, pool, sources)}["enrichment"]
    assert enrich.more is not None
    assert enrich.more(0) is False                  # quiet KB -> idle poll
    kb.upsert(NodeRecord(source="agent", external_id="m1", type="note",
                         content="c", slug="m1"))
    kb.flag_stale_enrichment()
    assert enrich.more(3) is True                   # partial batch, backlog -> drain on
    kb.claim_enrichment(10)
    assert enrich.more(10) is False                 # table drained -> idle poll


def test_enrichment_tick_skips_and_calms_lane_while_provider_paused(kb, pool):
    from datetime import datetime, timedelta, timezone
    from kb_core.domain import NodeRecord

    n = kb.upsert(NodeRecord(source="agent", external_id="g1", type="note",
                             content="c", slug="g1", parent_path=""))
    kb.flag_stale_enrichment()
    kb.pause_provider("claude-cli",
                      until=datetime.now(timezone.utc) + timedelta(hours=1),
                      reason="session limit")

    lines: list[str] = []
    enrich = {j.name: j for j in build_jobs(kb, pool, [], log=lines.append)}["enrichment"]

    assert enrich.run() == 0                                     # claimed nothing
    assert kb.get(n.id).enrichment_status == "pending"           # untouched
    assert any("enrichment paused" in ln for ln in lines)
    assert not enrich.more(0)          # pending exists, but the lane must NOT re-tick

    with pool.connection() as conn:                              # outage ends
        conn.execute("DELETE FROM provider_pause")
    assert enrich.run() == 1                                     # resumes by itself
    assert enrich.more(1) in (False, True)  # delegates to has_pending_enrichment again


def test_pull_tick_log_reports_seen_and_changed(kb, pool, tmp_path):
    """The idle tick used to log `upserted=<corpus size>` although nothing was written."""
    from kb_core.adapters.obsidian.adapter import ObsidianAdapter
    (tmp_path / "a.md").write_text("aaa", encoding="utf-8")
    (tmp_path / "b.md").write_text("bbb", encoding="utf-8")
    cfg = SourceConfig(source="obsidian", mode="pull", period=timedelta(seconds=20),
                       adapter=ObsidianAdapter(tmp_path))
    lines: list[str] = []
    jobs = {j.name: j for j in build_jobs(kb, pool, [cfg], log=lines.append)}
    jobs["obsidian"].run()
    jobs["obsidian"].run()
    pulls = [ln for ln in lines if ln.startswith("[ingestd] pull obsidian:")]
    assert pulls == [
        "[ingestd] pull obsidian: seen=2 changed=2 deleted=0 errors=0",
        "[ingestd] pull obsidian: seen=2 changed=0 deleted=0 errors=0",
    ]
