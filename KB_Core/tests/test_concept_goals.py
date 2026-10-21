from datetime import datetime, timedelta, timezone

from kb_core.domain import NodeRecord


def _note(kb, ext, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=f"body of {ext}", slug=ext, **kw))


def _seed_concepts(kb, specs):
    """specs: list of (name, [nodes]) -> {name: concept_id_str}."""
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": name, "prev_id": None, "parent_key": None, "level": 0,
         "name": name, "kind": "project", "description": "",
         "members": [(str(n.id), "memory", 1.0) for n in nodes],
         "embedding": None}
        for name, nodes in specs], [])
    kb.finish_concept_run(run, status="done", stats={})
    return {c["name"]: str(c["id"]) for c in kb.active_concepts()}


def _age_ingest_events(pool, node_id, days):
    """Backdate a node's ingest_log events — simulates work done `days` ago."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE ingest_log SET at = now() - make_interval(days => %s) "
            "WHERE node_id = %s", (days, node_id))


def test_current_is_membership_and_catalog_is_complete(kb, pool):
    fresh = _note(kb, "fresh")                       # upsert logs an event now
    old = _note(kb, "old")
    _age_ingest_events(pool, old.id, 30)
    ids = _seed_concepts(kb, [("Live", [fresh]), ("Dormant", [old])])
    prio = kb.concept_priority(window_days=14)
    assert prio[ids["Live"]]["current"] is True      # one event is enough
    assert prio[ids["Dormant"]]["current"] is False
    assert prio[ids["Dormant"]]["trend"] is None
    assert set(prio) == set(ids.values())            # nothing filtered out
    assert prio[ids["Live"]]["priority"] > prio[ids["Dormant"]]["priority"]


def test_event_at_counts_as_activity(kb, pool):
    met = _note(kb, "met", event_at=datetime.now(timezone.utc) - timedelta(days=2))
    _age_ingest_events(pool, met.id, 30)             # authored long ago, HAPPENED now
    ids = _seed_concepts(kb, [("Meetingful", [met])])
    assert kb.concept_priority(window_days=14)[ids["Meetingful"]]["current"] is True


def test_dormant_concepts_rank_by_importance(kb, pool):
    plain = _note(kb, "plain")
    vip = _note(kb, "vip")
    for n in (plain, vip):
        _age_ingest_events(pool, n.id, 30)
    ids = _seed_concepts(kb, [("Plain", [plain]), ("VIP", [vip])])
    kb.set_importance(vip.id, 5.0)
    prio = kb.concept_priority()
    assert prio[ids["VIP"]]["priority"] > prio[ids["Plain"]]["priority"]
    assert prio[ids["VIP"]]["importance"] == 5.0


def test_retrieval_floats_a_past_project_back_up(kb, pool):
    a = _note(kb, "proj_a")
    b = _note(kb, "proj_b")
    for n in (a, b):
        _age_ingest_events(pool, n.id, 30)
    ids = _seed_concepts(kb, [("A", [a]), ("B", [b])])
    before = kb.concept_priority()
    assert before[ids["A"]]["priority"] == before[ids["B"]]["priority"]
    kb.record_concept_access(ids["B"])               # the drill-in retrieval hook
    after = kb.concept_priority()
    assert after[ids["B"]]["priority"] > after[ids["A"]]["priority"]
    assert after[ids["B"]]["last_retrieved_at"] is not None


def test_trend_rising_vs_fading(kb, pool):
    riser = _note(kb, "riser")                       # only event: now -> rising
    fader = _note(kb, "fader")
    _age_ingest_events(pool, fader.id, 10)           # inside window, prior half only
    ids = _seed_concepts(kb, [("Riser", [riser]), ("Fader", [fader])])
    prio = kb.concept_priority(window_days=14)
    assert prio[ids["Riser"]]["trend"] == "rising"
    assert prio[ids["Fader"]]["current"] is True
    assert prio[ids["Fader"]]["trend"] == "fading"


def test_concepts_view_orders_by_priority_when_windowed(kb, pool):
    fresh = _note(kb, "v_fresh")
    old = _note(kb, "v_old")
    _age_ingest_events(pool, old.id, 30)
    _seed_concepts(kb, [("Cold", [old]), ("Hot", [fresh])])
    view = kb.concepts_view(window_days=14)
    assert [c["name"] for c in view["concepts"]] == ["Hot", "Cold"]
    assert view["concepts"][0]["goal"]["current"] is True
    # without a window the dashboard payload is unchanged: size order, no goal block
    plain = kb.concepts_view()
    assert "goal" not in plain["concepts"][0]
