"""The contract every retrieval system must satisfy.

capabilities() lets a benchmark harness plan a valid matrix before spending LLM
calls; applied_filters/ignored_filters on the result let it audit afterwards that
a strategy did not silently drop a facet and get scored as a win for it.
"""

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from kb_retrieval.contract import (CRAWL_CAPS, FILTER_NAMES, HYBRID_CAPS, Cost, Query,
                                   RetrievalResult, RetrievalStrategy, Row, Trace)


class _Fake:
    name = "fake"

    def capabilities(self):
        return HYBRID_CAPS

    def search(self, q):
        return RetrievalResult(rows=[], confidence={"level": "none", "n": 0},
                               applied_filters={}, ignored_filters=[],
                               cost=Cost(), trace=Trace())


def test_a_conforming_class_satisfies_the_protocol():
    assert isinstance(_Fake(), RetrievalStrategy)


def test_a_class_missing_capabilities_does_not_satisfy_it():
    class NoCaps:
        name = "x"

        def search(self, q):
            return None

    assert not isinstance(NoCaps(), RetrievalStrategy)


def test_capabilities_are_frozen():
    with pytest.raises(FrozenInstanceError):
        HYBRID_CAPS.filters = frozenset()


def test_hybrid_honours_every_filter_and_uses_no_llm():
    assert HYBRID_CAPS.filters == FILTER_NAMES
    assert HYBRID_CAPS.uses_llm is False
    assert HYBRID_CAPS.cost_class == "cheap"


def test_crawl_adds_anchor_entity_and_is_expensive():
    assert CRAWL_CAPS.filters == FILTER_NAMES | {"anchor_entity"}
    assert CRAWL_CAPS.uses_llm is True
    assert CRAWL_CAPS.cost_class == "expensive"


def test_query_defaults_match_the_deployed_search_defaults():
    q = Query(text="hello")
    assert q.limit == 20
    assert q.include_dead is True
    assert q.under is None


def test_cost_defaults_to_zero_so_a_cheap_strategy_reports_a_real_number():
    assert Cost().llm_calls == 0
    assert Cost().tokens_returned == 0


def test_row_carries_via_provenance():
    r = Row(id=uuid4(), path="a.b", title="t", similarity=0.5, rank=0.01, via=["flat"])
    assert r.via == ["flat"]


def test_filter_fields_and_filter_names_agree():
    """FILTER_FIELDS is the ORDERED tuple of None-defaulted keyword filters; the
    frozenset FILTER_NAMES is the same vocabulary plus include_dead, which is a bool
    with a True default and so cannot express "not set" as None. Two strategies
    building **filters dicts from two hand-copied tuples is how a facet quietly
    stops being passed by one of them."""
    from kb_retrieval.contract import FILTER_FIELDS
    assert set(FILTER_FIELDS) | {"include_dead"} == FILTER_NAMES
    assert "include_dead" not in FILTER_FIELDS


def test_estimate_tokens_floors_each_part_separately():
    """This is HybridStrategy's shipped arithmetic, not a rounder one:
    len(a)//4 + len(b)//4, NOT len(a+b)//4. The two differ whenever a part's length
    is not a multiple of 4, and changing it would move mode A's reported cost for no
    measured reason."""
    from kb_retrieval.contract import estimate_tokens
    assert estimate_tokens("abcde", "abcde") == 2      # 1 + 1, not 10//4 == 2 by luck
    assert estimate_tokens("abc", "abc") == 0          # 0 + 0, while 6//4 would be 1


def test_estimate_tokens_treats_none_as_empty():
    from kb_retrieval.contract import estimate_tokens
    assert estimate_tokens(None, None) == 0
    assert estimate_tokens(None, "a" * 8) == 2


def test_deterministic_crawl_caps_are_cheap_and_llm_free():
    """capabilities() exists so a benchmark can plan a valid matrix BEFORE spending
    expensive calls (contract.py's own docstring). A judge='none' crawl makes zero
    LLM calls, so reporting uses_llm=True would make a cheap-only sweep skip half of
    the 2x2 benchmark. Same filter set -- the pipeline is identical; only the
    LLM-dependent facts differ."""
    from kb_retrieval.contract import CRAWL_DETERMINISTIC_CAPS
    assert CRAWL_DETERMINISTIC_CAPS.filters == CRAWL_CAPS.filters
    assert CRAWL_DETERMINISTIC_CAPS.needs_embedding is True
    assert CRAWL_DETERMINISTIC_CAPS.uses_llm is False
    assert CRAWL_DETERMINISTIC_CAPS.cost_class == "cheap"


def test_trace_carries_a_stop_reason_and_a_rejected_admission_count():
    """Both are crawl disclosure fields. rejected_admissions counts indices a judge
    emitted outside the offered frontier -- the only shape a malformed or hostile
    admission can take, since a judge cannot emit a node id at all."""
    t = Trace()
    assert t.stop_reason is None
    assert t.rejected_admissions == 0


def test_trace_discloses_the_llm_status_and_a_cold_concept_layer():
    """The design's disclosure surface. `llm` is None for a crawl with no LLM tier at
    all -- not "ok", which would claim a successful call nobody made. Read
    `stale_concepts` together with `consolidated_at`: an empty trace.entry looks
    identical whether the crawl found nothing or the concept layer is cold."""
    t = Trace()
    assert t.llm is None
    assert t.stale_concepts is False


def test_label_vector_falls_back_to_the_retrieval_vector():
    assert Query(text="x").label_vector() is None
    assert Query(text="x", embedding=[1.0]).label_vector() == [1.0]
    assert Query(text="x", embedding=[1.0], label_embedding=[2.0]).label_vector() == [2.0]
