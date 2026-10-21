"""Reciprocal rank fusion -- ONE implementation, shared by every strategy.

Rank-based fusion exists precisely so two incomparable scores (ts_rank and cosine)
never need calibrating: only positions are used. Proving this once is what lets
both strategies share it.
"""

from uuid import UUID

from kb_core.knowledge_base import RRF_K
from kb_retrieval.fusion import rrf

A = UUID(int=1)
B = UUID(int=2)


def test_rrf_k_is_the_engine_constant_not_a_new_one():
    assert RRF_K == 60


def test_single_list_scores_by_position():
    got = rrf([[A, B]])
    assert got[A] == 1.0 / (60 + 1)
    assert got[B] == 1.0 / (60 + 2)


def test_membership_in_two_lists_sums():
    got = rrf([[A], [A]])
    assert got[A] == 1.0 / 61 + 1.0 / 61


def test_absent_from_a_list_contributes_zero_not_a_penalty():
    got = rrf([[A, B], [B]])
    # A appears once at rank 1; B at rank 2 and rank 1.
    assert got[A] == 1.0 / 61
    assert got[B] == 1.0 / 62 + 1.0 / 61
    assert got[B] > got[A]


def test_empty_lists_are_ignored():
    assert rrf([[], [A]]) == {A: 1.0 / 61}


def test_no_lists_yields_empty():
    assert rrf([]) == {}


def test_a_corroborated_mid_rank_row_can_outrank_a_flat_top_one():
    """The design's worked example: flat #1 in two lists = 0.0328; a crawl row at
    cosine rank 30 that was admitted first and corroborated = 0.0439."""
    flat_top = rrf([[A], [A]])[A]
    crawl = rrf([[*[UUID(int=i) for i in range(100, 129)], B], [B], [B]])[B]
    assert round(flat_top, 4) == 0.0328
    assert round(crawl, 4) == 0.0439
    assert crawl > flat_top


def test_duplicate_ids_within_one_list_take_the_best_position():
    got = rrf([[A, A]])
    assert got[A] == 1.0 / 61
