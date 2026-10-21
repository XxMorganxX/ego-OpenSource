"""The admission seam -- pure, no database."""

from uuid import UUID

import pytest

from kb_retrieval.contract import Query
from kb_retrieval.crawl.frontier import FRONTIER_ADMITTED, FrontierItem
from kb_retrieval.crawl.judge import (DEFAULT_HOP_JUDGE, HOP_JUDGES,
                                      DeterministicHopJudge, HopDecision, HopJudge,
                                      get_judge)


def _frontier(n):
    return [FrontierItem(idx=i, node_id=UUID(int=i + 1), family="concept",
                         parents=(UUID(int=1000),), menu_score=1.0, similarity=0.5)
            for i in range(n)]


def test_the_deterministic_judge_satisfies_the_protocol():
    """isinstance, NOT issubclass. A runtime_checkable Protocol carrying non-method
    members (`name`, `uses_llm`) raises TypeError under issubclass -- the same trap
    registry.py documents for RetrievalStrategy."""
    assert isinstance(DeterministicHopJudge(), HopJudge)
    with pytest.raises(TypeError):
        issubclass(DeterministicHopJudge, HopJudge)


def test_it_admits_the_first_frontier_admitted_rows_in_menu_order():
    d = DeterministicHopJudge().decide(Query(text="q"), _frontier(30), 1)
    assert d.admit == list(range(FRONTIER_ADMITTED))
    assert len(d.admit) == 12


def test_it_admits_everything_when_the_frontier_is_shorter_than_the_cap():
    d = DeterministicHopJudge().decide(Query(text="q"), _frontier(4), 1)
    assert d.admit == [0, 1, 2, 3]


def test_it_always_stops():
    """"Always stops" -- not "stops after some heuristic" -- is what makes this the
    SAME pipeline as an LLM judge with one function swapped. The hop loop, the budget
    checks and the marginal-yield test in strategy.py never change; this judge simply
    never asks for hop 2."""
    for hop in (1, 2):
        for n in (0, 5, 100):
            assert DeterministicHopJudge().decide(Query(text="q"), _frontier(n), hop).stop is True


def test_it_is_not_query_conditioned():
    """The machine-checkable form of what an LLM judge would add. The crawl still
    admits query-ROUTED rows -- concept and bridge membership was routed there BY the
    query's embedding -- but nothing here is query-CONDITIONED, which is exactly the
    gap an LLM judge closes."""
    f = _frontier(20)
    a = DeterministicHopJudge().decide(Query(text="how did we fix the stall"), f, 1)
    b = DeterministicHopJudge().decide(Query(text="totally different question"), f, 1)
    assert a == b


def test_get_judge_defaults_and_names_what_is_registered():
    """DEFAULT_HOP_JUDGE STAYS "none" -- a deliberate deviation from the original
    design's DEFAULT_HOP_JUDGE = "llm". ~15 tests call
    get_strategy("cluster-crawl", kb=kb) unpinned, and registry.py's DEFAULT_STRATEGY comment states that
    a default must never silently opt a caller into an expensive path. The MCP tool
    passed judge="llm" explicitly while the LLM tier existed; production is
    deterministic-only since the tier was deleted."""
    assert DEFAULT_HOP_JUDGE == "none"
    assert isinstance(get_judge(None), DeterministicHopJudge)
    assert isinstance(get_judge("none"), DeterministicHopJudge)
    assert set(HOP_JUDGES) == {"none"}
    with pytest.raises(ValueError) as exc:
        get_judge("supervisor")
    assert "supervisor" in str(exc.value) and "none" in str(exc.value)


def test_a_hop_decision_is_frozen_and_index_only():
    """`admit` is INDEX-only -- positions into the offered Frontier, never a node id
    (prompt-injection containment): the worst a hostile note can achieve is
    getting ITSELF admitted, and it still has to survive rank.py's four-way fusion to
    reach a slot."""
    from dataclasses import FrozenInstanceError
    d = HopDecision(admit=[0, 1], stop=True)
    assert d.note == ""
    with pytest.raises(FrozenInstanceError):
        d.stop = False


def test_a_judge_missing_uses_llm_fails_the_protocol():
    """The positive case above only proves a COMPLIANT judge satisfies HopJudge.
    Nothing in the suite proved the converse: that dropping `uses_llm` from a judge
    -- or from the Protocol itself -- would be caught. A fake with `name` and
    `decide` but no `uses_llm` must fail isinstance."""
    class _MissingUsesLLM:
        name = "missing-uses-llm"

        def decide(self, q, frontier, hop):
            return HopDecision(admit=[], stop=True)

    assert not isinstance(_MissingUsesLLM(), HopJudge)


def test_no_field_on_a_hop_decision_can_carry_a_node_id():
    """The structural form of the same claim: index-only is a property of the TYPE,
    not of one parser. `admit` is the only route from the model into the pipeline,
    and it is list[int].

    Matched on WORD BOUNDARIES, never substrings. A naive
    `any("id" in n for n in names)` is False the moment this dataclass gains
    `provider_tokens` -- "prov-ID-er" -- and the test fails on a field that carries
    an integer token count. Splitting on "_" is the same check without the
    accident."""
    from dataclasses import fields
    names = {f.name for f in fields(HopDecision)}
    assert "admit" in names
    banned = {"id", "node", "path"}
    offenders = [n for n in names if banned & set(n.split("_"))]
    assert offenders == [], offenders


def test_the_deterministic_judge_still_ignores_the_admitted_digest():
    """The Protocol gained a keyword; the rule did not change. DeterministicHopJudge
    must produce the identical decision with and without a digest, or the two modes
    stop being "the same pipeline with decide() replaced by a rule"."""
    f = _frontier(20)
    bare = DeterministicHopJudge().decide(Query(text="q"), f, 1)
    with_digest = DeterministicHopJudge().decide(Query(text="q"), f, 1,
                                                 admitted=("a", "b"))
    assert bare == with_digest


def test_llm_judge_is_gone_and_the_error_names_the_registry():
    with pytest.raises(ValueError, match="unknown hop judge 'llm'.*none"):
        get_judge("llm")
