"""Tests for the LLM judge gate: merge_judge module and KnowledgeBase judge/staging methods."""
from kb_core.merge_judge import build_judge_prompt, parse_judge, JUDGE_SCHEMA


class ScriptedJudge:
    def __init__(self, replies): self._r = list(replies); self.calls = 0
    def embed(self, texts): raise AssertionError("not used")
    def complete(self, system, user, *, schema=None):
        self.calls += 1
        assert schema == JUDGE_SCHEMA
        return self._r.pop(0)


def test_parse_judge_reads_same_and_reason():
    assert parse_judge('{"same": true, "reason": "same uni"}') == (True, "same uni")


def test_merge_prompt_handles_aliases_and_abstract_concepts():
    system, user = build_judge_prompt(
        {"name": "TDD", "aliases": [], "mentions": ["Use TDD for the feature"]},
        {"name": "test-driven development", "aliases": [],
         "mentions": ["Follow test-driven development"]},
    )

    assert "concept" in system.lower()
    assert "abbreviation" in system.lower()
    assert "merely related" in system.lower()
    assert "TDD" in user and "test-driven development" in user


def test_same_pair_is_staged(kb):
    a = kb.upsert_entity("Acme"); b = kb.upsert_entity("Acme University")
    cand = {"left_id": a.node.id, "right_id": b.node.id, "kind": "instance", "distance": 0.1}
    client = ScriptedJudge(['{"same": true, "reason": "same university"}'])
    res = kb.judge_and_stage_candidates(client, [cand])
    assert res == {"staged": 1, "blocked": 0}
    # Check the pending count directly rather than through list_merge_suggestions
    with kb._pool.connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM entity_merge_candidates WHERE status='pending'"
        ).fetchone()["n"]
    assert n == 1


def test_not_same_pair_is_blocked(kb):
    a = kb.upsert_entity("Apple"); b = kb.upsert_entity("Apple Inc")
    cand = {"left_id": a.node.id, "right_id": b.node.id, "kind": "instance", "distance": 0.1}
    client = ScriptedJudge(['{"same": false, "reason": "fruit vs company"}'])
    res = kb.judge_and_stage_candidates(client, [cand])
    assert res == {"staged": 0, "blocked": 1}
    with kb._pool.connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM entity_merge_blocks WHERE origin='judge'"
        ).fetchone()["n"]
    assert n == 1


def test_judge_prompt_includes_pair_evidence():
    from kb_core.merge_judge import build_judge_prompt
    _sys, usr = build_judge_prompt(
        {"name": "A"}, {"name": "B"},
        {"co_mentions": 3, "shared_co_entities": 5, "via": "name"})
    assert "3 shared memories" in usr
    assert "share 5 other co-mentioned entities" in usr
    assert "surfaced by name similarity" in usr
    # without evidence the section is absent (backward-compatible)
    _sys, usr = build_judge_prompt({"name": "A"}, {"name": "B"})
    assert "Pair evidence" not in usr


def test_pair_judge_evidence_counts(kb):
    from kb_core.domain import NodeRecord
    a = kb.upsert_entity("Alpha")
    b = kb.upsert_entity("Beta")
    other = kb.upsert_entity("Gamma")
    both = kb.upsert(NodeRecord(source="agent", external_id="pe1", type="note",
                                content="a and b together", slug="pe1"))
    only_a = kb.upsert(NodeRecord(source="agent", external_id="pe2", type="note",
                                  content="a alone", slug="pe2"))
    only_b = kb.upsert(NodeRecord(source="agent", external_id="pe3", type="note",
                                  content="b alone", slug="pe3"))
    for mem, ent in ((both, a), (both, b), (only_a, a), (only_b, b),
                     (only_a, other), (only_b, other)):
        kb.link_entity(mem.id, ent.node.id, "mentions")
    ev = kb._pair_judge_evidence(a.node.id, b.node.id)
    assert ev["co_mentions"] == 1                # the one memory linking both
    assert ev["shared_co_entities"] == 1         # Gamma, reachable from each side


def test_stage_reason_carries_via_provenance(kb):
    left = kb.upsert_entity("Avery", categories=["person"])
    right = kb.upsert_entity("Avery Quinn", categories=["person"])
    judge = ScriptedJudge(['{"same": true, "reason": "same person"}'])
    cands = [{"left_id": left.node.id, "right_id": right.node.id,
              "kind": "instance", "distance": 0.5, "via": "name"}]
    res = kb.judge_and_stage_candidates(judge, cands)
    assert res["staged"] == 1
    sug = kb.list_merge_suggestions()
    assert sug and sug[0]["judge_reason"].startswith("[name] ")
