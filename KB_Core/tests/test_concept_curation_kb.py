"""KB methods for durable concept curation."""
import pytest
from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS


def _mem(kb, eid):
    return kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                                content="c", slug=eid, title=eid))


def _concept(kb, name, members):
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [{"key": name, "prev_id": None, "parent_key": None,
                                "level": 0, "name": name, "kind": "project",
                                "description": None, "members": members,
                                "embedding": None}], [])
    kb.finish_concept_run(run, status="done", stats={})
    return next(c for c in kb.active_concepts() if c["name"] == name)


def test_constraints_roundtrip_canonical_and_contradiction(kb):
    a, b = _mem(kb, "a"), _mem(kb, "b")
    row = kb.add_concept_constraint("cannot_link", b.id, a.id, note="they differ")
    assert str(row["left_id"]) == min(str(a.id), str(b.id))     # canonical order
    again = kb.add_concept_constraint("cannot_link", a.id, b.id)
    assert again["id"] == row["id"]                              # idempotent duplicate
    with pytest.raises(ValueError):
        kb.add_concept_constraint("must_link", a.id, b.id)       # contradiction
    with pytest.raises(ValueError):
        kb.add_concept_constraint("cannot_link", a.id, a.id)     # self-pair
    assert [c["note"] for c in kb.concept_constraints()] == ["they differ"]
    assert kb.delete_concept_constraint(row["id"]) is True
    assert kb.concept_constraints() == []


def test_concept_activity_counts_only_authored_changes_in_window(kb, pool):
    """upsert logs node_created -> counted. Pipeline events (enrichment_done) and
    events outside the window must NOT count — the whole point of the rollup is
    surviving timestamp pollution."""
    m1, m2 = _mem(kb, "a"), _mem(kb, "b")
    c = _concept(kb, "C", [(str(m1.id), "memory", 1.0),
                           (str(m2.id), "memory", 0.9)])
    act = kb.concept_activity(days=7)[str(c["id"])]
    assert act["changed"] == 2 and act["last_change"] is not None
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO ingest_log (at, event, source, external_id, node_id, path) "
            "VALUES (now(), 'enrichment_done', 'agent', 'x', %s, 'p'), "
            "       (now() - interval '30 days', 'node_updated', 'agent', 'y', %s, 'p')",
            (m1.id, m1.id))
    act = kb.concept_activity(days=7)[str(c["id"])]
    assert act["changed"] == 2                       # neither extra row counted
    view = kb.concepts_view(activity_days=7)["concepts"]
    assert view[0]["activity"]["changed"] == 2
    assert "activity" not in kb.concepts_view()["concepts"][0]   # param-gated


def test_separation_roundtrip_canonical_and_idempotent(kb):
    m1, m2 = _mem(kb, "a"), _mem(kb, "b")
    c1 = _concept(kb, "C1", [(str(m1.id), "memory", 1.0)])
    c2 = _concept(kb, "C2", [(str(m2.id), "memory", 1.0)])
    row = kb.add_concept_separation(c2["id"], c1["id"], note="unrelated")
    assert str(row["left_id"]) == min(str(c1["id"]), str(c2["id"]))  # canonical order
    again = kb.add_concept_separation(c1["id"], c2["id"])
    assert again["id"] == row["id"]                                  # idempotent
    with pytest.raises(ValueError):
        kb.add_concept_separation(c1["id"], c1["id"])                # self-pair
    with pytest.raises(LookupError):
        kb.add_concept_separation(c1["id"],
                                  "00000000-0000-0000-0000-000000000000")
    listed = kb.concept_separations()
    assert [s["note"] for s in listed] == ["unrelated"]
    assert {listed[0]["left_name"], listed[0]["right_name"]} == {"C1", "C2"}
    assert kb.delete_concept_separation(row["id"]) is True
    assert kb.concept_separations() == []


def test_separation_rules_carry_members_strongest_first(kb):
    m1, m2, m3 = _mem(kb, "a"), _mem(kb, "b"), _mem(kb, "c")
    c1 = _concept(kb, "C1", [(str(m1.id), "memory", 0.4),
                             (str(m2.id), "memory", 1.0)])
    c2 = _concept(kb, "C2", [(str(m3.id), "memory", 1.0)])
    kb.add_concept_separation(c1["id"], c2["id"])
    rules = kb.concept_separation_rules()
    c1_members = next(r for r in (rules[0]["left_members"],
                                  rules[0]["right_members"]) if len(r) == 2)
    assert [str(n) for n in c1_members] == [str(m2.id), str(m1.id)]  # score-ordered


def test_label_lock_and_status(kb):
    m = _mem(kb, "m1")
    c = _concept(kb, "Old name", [(str(m.id), "memory", 1.0)])
    out = kb.set_concept_label(c["id"], name="Vandelay", kind="project")
    assert out["name"] == "Vandelay" and out["label_locked"] is True
    with pytest.raises(ValueError):
        kb.set_concept_label(c["id"], kind="banana")
    dismissed = kb.set_concept_status(c["id"], "dismissed")
    assert dismissed["status"] == "dismissed"
    assert kb.active_concepts() == []                            # hidden from the UI read
    assert {x["status"] for x in kb.matchable_concepts()} == {"dismissed"}
    assert kb.set_concept_status(c["id"], "active")["status"] == "active"


def test_member_override_applies_immediately_and_persists_rule(kb, pool):
    m1, m2 = _mem(kb, "m1"), _mem(kb, "m2")
    c = _concept(kb, "C", [(str(m1.id), "memory", 1.0), (str(m2.id), "memory", 0.8)])
    kb.override_concept_member(c["id"], m2.id, "remove", note="not related")
    fresh = next(x for x in kb.active_concepts() if x["id"] == c["id"])
    assert fresh["member_count"] == 1                            # applied now
    with pool.connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM concept_member_overrides").fetchone()["n"]
    assert n == 1                                                # and durable
    assert kb.clear_concept_member_override(c["id"], m2.id) is True


def test_roster_matches_entities_by_word(kb):
    ent = kb.upsert_entity("Vandelay", categories=["project"])
    m = _mem(kb, "m1")
    kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)
    _concept(kb, "Acme club", [(str(m.id), "memory", 1.0)])
    roster = kb.concept_curation_roster("split vandelay from the acme stuff")
    assert [e["name"] for e in roster["entities"]] == ["Vandelay"]
    assert [c["name"] for c in roster["concepts"]] == ["Acme club"]


def test_roster_does_not_treat_underscore_as_wildcard(kb):
    kb.upsert_entity("foo_bar", categories=["tool"])
    kb.upsert_entity("fooxbar", categories=["tool"])
    roster = kb.concept_curation_roster("about foo_bar please")
    assert [e["name"] for e in roster["entities"]] == ["foo_bar"]


def test_roster_includes_dismissed_concepts_tagged(kb):
    m = _mem(kb, "m1")
    c = _concept(kb, "Old thing", [(str(m.id), "memory", 1.0)])
    kb.set_concept_status(c["id"], "dismissed")
    roster = kb.concept_curation_roster("restore old thing")
    assert [x["name"] for x in roster["concepts"]] == ["Old thing (dismissed)"]
