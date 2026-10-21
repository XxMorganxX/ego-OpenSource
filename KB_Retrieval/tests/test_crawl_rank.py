"""Rank assembly -- pure, no database.

Two of these are the sharpest doctrine tests in the plan: assemble()'s signature
does not even ACCEPT a menu_score, so "structure promoted a row" is unrepresentable.
If a future edit threads a frontier menu score into ranking, these start failing the
moment it changes an outcome.
"""

from uuid import UUID

from kb_retrieval.crawl.rank import assemble, corrob_rank_list, similarity_rank_list

A, B, C = UUID(int=1), UUID(int=2), UUID(int=3)


def test_a_high_menu_score_row_cannot_outrank_a_row_absent_from_every_list():
    """B is admitted (L_admit) and could carry a sky-high frontier menu_score -- but
    that number is never passed into assemble() at all.

    A: flat rank1 (1/61) + sim rank1 (1/61)   = 0.03279
    B: sim rank2 (1/62) + admit rank1 (1/61)  = 0.03252
    Admission ALONE, uncorroborated, does not beat flat's top-1. That bound is the
    design's own, and it is what makes "nothing is ever removed" a real floor."""
    order, fused = assemble(l_flat=[A], l_sim=[A, B], l_admit=[B], l_corrob=[],
                            similarity={A: 0.9, B: 0.1}, path_order={A: 0, B: 1})
    assert order[0] == A
    assert round(fused[A], 5) == 0.03279
    assert round(fused[B], 5) == 0.03252


def test_admitted_and_corroborated_row_can_outrank_flat_top_one_but_only_then():
    """The positive control for the same doctrine, and the design's own worked
    example: admission alone is not enough (previous test); admission PLUS
    corroboration is. Judgment combined with a permitted structural signal CAN win;
    a menu score never can."""
    order, fused = assemble(l_flat=[A], l_sim=[B, A], l_admit=[B], l_corrob=[B],
                            similarity={A: 0.9, B: 0.5}, path_order={A: 0, B: 1})
    assert order[0] == B
    assert round(fused[B], 5) == 0.04918     # 3/61
    assert round(fused[A], 5) == 0.03252     # 1/61 + 1/62


def test_similarity_rank_list_ties_do_not_depend_on_input_order():
    """THE REGRESSION THIS FILE EXISTS FOR. strategy.py feeds this a Python set,
    whose iteration order is not stable across runs. A tiebreak that is a constant
    (the original draft used the literal "") leaves ties ordered by arrival -- and
    RRF turns a position into a score, so "byte-identical on repeat" (a stated
    benchmark requirement) silently stops holding."""
    sim = {A: 0.5, B: 0.5}
    assert similarity_rank_list([A, B], sim) == similarity_rank_list([B, A], sim)
    assert similarity_rank_list([B, A], sim) == [A, B]


def test_similarity_rank_list_omits_scoreless_rows_instead_of_tailing_them():
    """A row can be scoreless for three innocent reasons -- no
    query embedding, no vector on the note, or similarity_for() omitted it -- and
    L_flat, L_admit or L_corrob can still carry it, so dropping it from L_sim does
    not drop it from the pool. This USED TO tail every scoreless row on, sorted by
    `str(ident)`, at full RRF weight -- with no query embedding that put the WHOLE
    POOL in UUID order (the design: an embedder outage means "no L_sim", not
    "L_sim in id order"), and in the normal path it did the same to any individually
    unembedded row, which is exactly the population the crawl arm exists to reach.
    Now it is omitted outright, exactly matching corrob_rank_list's reasoning for
    omitting zero-count rows: rrf() already treats absence as a 0 contribution, so
    an included tail buys nothing."""
    got = similarity_rank_list([A, B, C], {A: None, B: 0.2})   # A scoreless, C absent
    assert got == [B]              # A and C are both OMITTED, not tailed on


def test_similarity_rank_list_ties_among_scored_rows_still_break_by_id():
    """The tiebreak that matters now applies only to rows that DO have a score."""
    sim = {A: 0.5, B: 0.5, C: None}
    assert similarity_rank_list([C, B, A], sim) == [A, B]
    assert similarity_rank_list([A, B, C], sim) == [A, B]


def test_corrob_rank_list_omits_zero_count_rows_and_breaks_ties_by_id():
    """A node with no bridge parents is omitted rather than tailed on at count 0:
    rrf() already treats absence as a 0 contribution, and an included zero tail would
    need an arbitrary tie order for no benefit."""
    got = corrob_rank_list({A: {C}, B: {A, C}, C: set()})
    assert got == [B, A]


def test_corrob_rank_list_ties_break_by_id_not_by_set_iteration():
    got = corrob_rank_list({B: {C}, A: {C}})
    assert got == [A, B]


def test_assemble_with_only_l_sim_still_returns_every_id():
    """When every id passed in l_sim is actually SCORED (as here), `fused` contains
    all of them regardless of which other lists are empty -- l_sim no longer covers
    the whole pool unconditionally (similarity_rank_list omits scoreless ids), so
    this is no longer a guarantee about l_sim itself, only about whatever
    ids it happens to carry. Dropping empty lists before calling rrf() is what makes
    the 4-way-vs-3-way ablation a one-line change at the CALL SITE --
    comment out a list, nothing else moves."""
    order, fused = assemble(l_flat=[], l_sim=[A, B], l_admit=[], l_corrob=[],
                            similarity={A: 0.9, B: 0.1}, path_order={A: 0, B: 1})
    assert order == [A, B]
    assert set(fused) == {A, B}


def test_assemble_pool_floor_gives_an_otherwise_signalless_id_a_zero_score_not_omission():
    """The scoreless-omission and l_admit-gate fixes interact: an id can be a member of
    ZERO of the four lists (unembedded, not flat, not a bridge parent, and -- for a
    non-LLM judge -- not carried by l_admit either), and rrf() then has no entry for
    it at all. The design ("nothing admitted is ever removed") requires it to still
    surface, so `pool` is where strategy.py asserts "this id was admitted regardless
    of what ranked it" -- and it must NOT let that admission rank the row above a
    real, if small, score. C's genuine 1/(k+1) contribution from l_sim must still
    beat the floor, and the floor value must be exactly 0.0, not some positive
    placeholder."""
    order, fused = assemble(l_flat=[], l_sim=[C], l_admit=[], l_corrob=[],
                            similarity={C: 0.1, A: None}, path_order={A: 0, C: 1},
                            pool=[A, C])
    assert order == [C, A]
    assert fused[C] > 0.0
    assert fused[A] == 0.0


def test_assemble_without_pool_omits_a_signalless_id_exactly_as_before():
    """The `pool` floor is opt-in: a caller (or an existing test) that never passes
    it gets exactly the pre-fix behaviour -- an id absent from all four lists is
    absent from `fused` too, not silently promoted to a floor entry it never asked
    for."""
    order, fused = assemble(l_flat=[], l_sim=[C], l_admit=[], l_corrob=[],
                            similarity={C: 0.1, A: None}, path_order={A: 0, C: 1})
    assert order == [C]
    assert set(fused) == {C}


def test_assemble_on_an_empty_pool_returns_empty_rather_than_raising():
    """Mirrors RetrievalStrategy's "MUST NOT raise for an empty result" contract."""
    assert assemble(l_flat=[], l_sim=[], l_admit=[], l_corrob=[],
                    similarity={}, path_order={}) == ([], {})


def test_assemble_tiebreak_on_exact_fused_score_tie_orders_by_similarity_desc():
    """The similarity-DESC leg of assemble()'s tiebreak only ever runs when two ids
    land on an EXACT fused-score tie -- any distinct fused score already decides the
    order and the tiebreak tuple is never consulted. l_flat=[A, B] and l_sim=[B, A]
    is a swapped rank-1/rank-2 pair, so A's 1/(k+1)+1/(k+2) equals B's
    1/(k+2)+1/(k+1) exactly. With that tie forced, only the similarity term can
    decide -- A's 0.9 must outrank B's 0.1."""
    order, fused = assemble(l_flat=[A, B], l_sim=[B, A], l_admit=[], l_corrob=[],
                            similarity={A: 0.9, B: 0.1}, path_order={A: 1, B: 0})
    assert round(fused[A], 5) == round(fused[B], 5)
    assert order == [A, B]


def test_assemble_tiebreak_on_similarity_tie_too_falls_through_to_path_order():
    """Once fused score AND similarity both tie, the spec says path_order (server
    ORDER BY path) breaks it -- but that branch only executes if both prior keys are
    equal. Same swapped-rank fused tie as above, plus an equal similarity for both
    ids, forces the comparison down to path_order alone: B's smaller path_order (0)
    must sort before A's (1) even though A was inserted first everywhere above."""
    order, fused = assemble(l_flat=[A, B], l_sim=[B, A], l_admit=[], l_corrob=[],
                            similarity={A: 0.5, B: 0.5}, path_order={A: 5, B: 2})
    assert round(fused[A], 5) == round(fused[B], 5)
    assert order == [B, A]


def test_assemble_tiebreak_similarity_none_sorts_last_not_first():
    """NULLS LAST is only observable at a fused-score tie: an id with no similarity
    must still lose to a tied id that HAS one, never win by an accident of numeric
    comparison against None. A's similarity is deliberately NEGATIVE (cosine can be):
    a mutant that collapses the None-branch tag onto the same tag as the real-value
    branch (treating None as similarity 0.0) would let None's 0.0 beat A's negative
    value under plain numeric comparison -- a positive A similarity would not expose
    that. Same swapped-rank fused tie; A must still sort first regardless of
    path_order favoring B."""
    order, fused = assemble(l_flat=[A, B], l_sim=[B, A], l_admit=[], l_corrob=[],
                            similarity={A: -0.3, B: None}, path_order={A: 9, B: 0})
    assert round(fused[A], 5) == round(fused[B], 5)
    assert order == [A, B]
