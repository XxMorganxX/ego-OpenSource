"""Pure-function tests for the shadow harness: no DB, no network, no embeds. Run with the
engine's venv python (never `uv run`):
   cd evals/retrieval && ../../KB_Core/.venv/bin/python -m pytest tests/test_shadow.py -q -p no:cacheprovider"""
import json
import math
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shadow as S  # noqa: E402
import shadow_bench as B  # noqa: E402
import shadow_confidence as C  # noqa: E402


# ---------------------------------------------------------------- BM25 math
def test_bm25_idf_decreases_with_document_frequency_and_stays_positive():
    n = 1000
    rare, common, everywhere = S.bm25_idf(2, n), S.bm25_idf(400, n), S.bm25_idf(n, n)
    assert rare > common > everywhere > 0
    assert math.isclose(S.bm25_idf(0, n), math.log((n + 0.5) / 0.5 + 1.0))


def test_bm25_tf_saturates_at_k1_plus_one_and_penalises_long_documents():
    w1 = S.bm25_tf_weight(1, 1000, 1000)
    w10 = S.bm25_tf_weight(10, 1000, 1000)
    w1000 = S.bm25_tf_weight(1000, 1000, 1000)
    assert w1 < w10 < w1000 < 1.2 + 1.0 + 1e-9          # k1 + 1 is the ceiling
    assert S.bm25_tf_weight(3, 4000, 1000) < S.bm25_tf_weight(3, 1000, 1000)   # longer doc, same tf
    assert math.isclose(S.bm25_tf_weight(3, 1000, 1000, b=0.0), S.bm25_tf_weight(3, 9000, 1000, b=0.0))


def test_bm25_score_is_the_sum_over_matched_query_lexemes_only():
    idf = {"orchard": 2.0, "harvest": 1.0}
    tf = {"orchard": 3, "harvest": 1, "unrelated": 50}
    expect = 2.0 * S.bm25_tf_weight(3, 500, 1000) + 1.0 * S.bm25_tf_weight(1, 500, 1000)
    assert math.isclose(S.bm25_score(tf, idf, 500, 1000), expect)


# ---------------------------------------------------------------- RRF fusion + tiebreak
def test_rrf_fuse_sums_weighted_reciprocal_ranks_and_ignores_duplicates():
    fused = S.rrf_fuse([(["a", "b", "a"], 1.0), (["b", "c"], 0.5)], k=60)
    assert math.isclose(fused["a"], 1 / 61)               # second occurrence ignored
    assert math.isclose(fused["b"], 1 / 62 + 0.5 / 61)
    assert math.isclose(fused["c"], 0.5 / 62)             # absent from list 1: no penalty


def test_order_fused_breaks_ties_by_server_path_order_and_sorts_unknown_paths_last():
    scores = {"x": 0.5, "y": 0.5, "z": 0.9, "w": 0.5}
    assert S.order_fused(scores, {"y": 0, "x": 1}) == ["z", "y", "x", "w"]


# ---------------------------------------------------------------- dedup operator
def test_collapse_keeps_better_ranked_member_and_refills_from_the_pool():
    ordered = ["a", "b", "c", "d", "e"]
    new, pairs = S.collapse_near_duplicates(ordered, [("c", "a", 0.99)], depth=4)
    assert new == ["a", "b", "d", "e", "c"]               # c collapsed under a, e refills
    assert pairs == [("a", "c", 0.99)]


def test_collapse_ignores_pairs_outside_depth_and_a_dropped_member_cannot_absorb():
    ordered = ["a", "b", "c", "d"]
    new, pairs = S.collapse_near_duplicates(ordered, [("a", "b", 0.98), ("b", "c", 0.97), ("a", "d", 0.99)], depth=3)
    assert "d" not in {p[1] for p in pairs}               # d is beyond depth 3
    assert pairs == [("a", "b", 0.98)]                    # b dropped; b cannot then absorb c
    assert new == ["a", "c", "d", "b"]


def test_collapse_is_identity_without_pairs():
    assert S.collapse_near_duplicates(["a", "b"], [], depth=10) == (["a", "b"], [])


# ---------------------------------------------------------------- metrics
def test_must_recall_and_first_hit_rank():
    ids = ["n1", "n2", "n3", "n4"]
    assert S.must_recall(ids, ["n2", "n9"], k=2) == 0.5
    assert S.first_hit_rank(ids, ["n3", "n4"], k=4) == 3
    assert S.first_hit_rank(ids, ["n9"], k=4) is None


def test_group_recall_counts_a_mirror_group_once_and_capped_recall_caps_at_k():
    groups = {"g1": {"copy-a", "copy-b"}, "g2": {"solo"}, "g3": {"far"}}
    ids = ["copy-a", "copy-b", "x", "solo"]
    assert S.group_recall(ids, groups, k=4) == (2, 3)
    assert S.capped_recall(ids, groups, k=2) == 0.5        # top-2 holds one group; denominator min(3, 2)
    assert S.capped_recall(["copy-b", "solo"], groups, k=2) == 1.0   # page full of gold
    assert S.capped_recall(["x"], {}, k=5) == 0.0


def test_graded_ndcg_is_one_for_ideal_order_and_credits_a_group_once():
    gains = {"g1": 2, "g2": 1}
    member_group = {"a": "g1", "a2": "g1", "b": "g2"}
    assert math.isclose(S.graded_ndcg(["a", "b"], gains, member_group, k=10), 1.0)
    assert math.isclose(S.graded_ndcg(["a", "a2", "b"], gains, member_group, k=10),
                        (2 / math.log2(2) + 1 / math.log2(4)) / (2 / math.log2(2) + 1 / math.log2(3)))
    assert S.graded_ndcg(["zzz"], gains, member_group, k=10) == 0.0
    assert S.graded_ndcg(["a"], {}, {}, k=10) == 0.0


# ---------------------------------------------------------------- as-of admissibility
def test_admissible_at_rules():
    at = "2026-06-01T12:00:00+00:00"
    assert S.admissible_at("2026-05-31T00:00:00+00:00", at)
    assert S.admissible_at(at, at)                                   # equal instant admitted
    assert not S.admissible_at("2026-06-01T12:00:01+00:00", at)
    assert S.admissible_at("2026-07-01", None)                       # no as-of: everything admitted
    assert not S.admissible_at(None, at)                             # unknown ingestion: excluded
    assert S.admissible_at(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2))   # naive = UTC


def test_admissible_at_reads_the_log_when_a_recreate_reset_ingested_at():
    at = "2026-06-01T12:00:00+00:00"
    recreated = "2026-06-10T00:00:00+00:00"                          # ingested_at after the recreate
    life = [("2026-05-01T00:00:00+00:00", "node_created"),
            ("2026-06-09T00:00:00+00:00", "node_deleted"),
            (recreated, "node_created")]
    assert S.admissible_at(recreated, at, life)                       # existed at `at`, before the reset
    assert not S.admissible_at(recreated, "2026-06-09T12:00:00+00:00", life)   # in the delete gap
    assert not S.admissible_at(recreated, "2026-04-01T00:00:00+00:00", life)   # before its creation
    assert S.admissible_at(recreated, at, [("2026-06-09T00:00:00+00:00", "node_deleted"),
                                           (recreated, "node_created")])       # predates the log
    assert not S.admissible_at(recreated, at, [(recreated, "node_created")])   # genuinely new


def test_admissible_at_ignores_the_log_once_ingested_before_asof():
    # a node still present whose last (re)creation precedes `asof` has existed ever since
    assert S.admissible_at("2026-05-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00",
                           [("2026-07-01T00:00:00+00:00", "node_created")])


def test_traffic_groups_grades_by_best_member_and_honours_admissibility():
    item = {"expected": {"memories": [
        {"id": "m1", "grade": 2, "group": "doc-A"}, {"id": "m2", "grade": 1, "group": "doc-A"},
        {"id": "m3", "grade": 1, "group": "doc-B"}, {"id": "m4", "grade": 2, "group": "doc-C"}]}}
    g2, gains, member_group, g1 = S.traffic_groups(item, {"m1": True, "m2": True, "m3": True, "m4": False})
    assert g2 == {"doc-A": {"m1", "m2"}}
    assert g1 == {"doc-B": {"m3"}}
    assert gains == {"doc-A": 2, "doc-B": 1}
    assert member_group == {"m1": "doc-A", "m2": "doc-A", "m3": "doc-B"}
    assert "doc-C" not in gains                                       # created after the trace


# ---------------------------------------------------------------- query normalisation / args
def test_normalize_query_turns_joined_tokens_into_phrases_and_strips_accents():
    assert S.normalize_query("orchard-labs résumé", "query") == '"orchard labs" resume'
    assert S.normalize_query("Repo_Name docs", "both") == '"Repo Name" docs'
    assert S.normalize_query("budget -vendor", "query") == "budget -vendor"       # negation untouched
    assert S.normalize_query("kb-mcp", None) == "kb-mcp"


def test_split_terms_keeps_phrases_whole_for_the_or_join():
    assert S.or_join(S.split_terms('"orchard labs" harvest plan')) == '"orchard labs" OR harvest OR plan'


class _FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _FakeConn:
    """Answers the OR-fallback probe with `hit` and records every statement."""
    def __init__(self, hit=False):
        self.hit, self.sql = hit, []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        return _FakeCursor({"hit": self.hit})


def _fake_kb():
    imp = SimpleNamespace(recency_floor=0.5, recency_half_life_seconds=86400.0)
    return SimpleNamespace(_policy=SimpleNamespace(importance=imp))


def test_fallback_terms_is_the_engines_whitespace_split_on_the_production_gate():
    q = '"orchard labs" harvest plan'
    assert S.fallback_terms(q, S.KNOB_DEFAULTS) == q.split()                     # engine: query.split()
    assert S.fallback_terms(q, {**S.KNOB_DEFAULTS, "lexnorm": "query"}) == S.split_terms(q)
    assert S.fallback_terms(q, {**S.KNOB_DEFAULTS, "fts_mode": "or_all"}) == S.split_terms(q)


def test_prod_or_fallback_gate_counts_and_joins_like_the_engine():
    kw, k = S.norm_args({})[0], {**S.KNOB_DEFAULTS, "_limit": 20}
    cap = S._engine().OR_FALLBACK_MAX_WORDS
    long_q = '"alpha bravo charlie delta" echo foxtrot golf hotel india'
    assert len(long_q.split()) > cap >= len(S.split_terms(long_q))             # the divergent shape
    conn = _FakeConn(hit=False)
    assert S.fts_query_text(_fake_kb(), conn, long_q, kw, k) == (long_q, False)   # over the cap: no fallback
    assert conn.sql == []                                                         # ... and no probe
    short_q = '"orchard labs" harvest plan'
    assert S.fts_query_text(_fake_kb(), _FakeConn(hit=False), short_q, kw, k) == (
        '"orchard OR labs" OR harvest OR plan', True)                             # engine's OR string
    assert S.fts_query_text(_fake_kb(), _FakeConn(hit=True), short_q, kw, k) == (short_q, False)


def test_query_ngrams_drops_stopwords_and_can_require_proper_nouns():
    grams = S.query_ngrams("the Orchard harvest of Avery's plan", caps_only=False)
    assert "orchard" in grams and "harvest" in grams and "the" not in grams and "of" not in grams
    assert "orchard harvest" in grams
    caps = S.query_ngrams("the Orchard harvest of Avery's plan", caps_only=True)
    assert "harvest" not in caps and "orchard harvest" in caps and "avery" in caps


def test_norm_args_mirrors_the_tool_defaults_and_clamps():
    kw, limit = S.norm_args({"limit": 999, "since": "2026-05-01", "kind": "runbook"})
    assert limit == S.MAX_LIMIT and kw["include_dead"] is True and kw["kind"] == "runbook"
    assert kw["since"].tzinfo is not None and kw["until"] is None
    assert S.norm_args({})[1] == S.DEFAULT_LIMIT
    assert S.norm_args({"include_dead": False})[0]["include_dead"] is False


# ---------------------------------------------------------------- variant registry
def test_variant_registry_is_well_formed():
    # the bench always forces the replica path: only KNOB_DEFAULTS keys with allowed values
    for name in B.REQUIRED_VARIANTS:
        assert name in B.VARIANTS, name
    assert B.VARIANTS["baseline"]["knobs"] == {}   # production defaults -- now instructed (shadow_search's own default)
    for name, v in B.VARIANTS.items():
        assert set(v) == {"knobs", "note"}, name
        S.validate_knobs(B.knobs_for(name, 20))
        assert v["note"]
    fin = B.VARIANTS["p1_bm25_orIE_noaccess"]["knobs"]
    assert fin["query_prefix"] == S.PREFIX_P1 and fin["fts_score"] == "bm25" and fin["importance"] == "no_access"
    assert B.knobs_for("or_if_few_scoped_bm25", 8)["or_few_k"] == 8
    assert "or_few_k" not in B.knobs_for("baseline", 8)


def test_historically_bare_levers_are_pinned_to_bare_not_the_instructed_default():
    # Every variant that predates the instruction must carry an EXPLICIT query_prefix
    # (BARE or one of the p1/p2/p3 instructions) rather than silently inheriting
    # KNOB_DEFAULTS' now-instructed default -- a missing key below is a KeyError, not
    # a pass. bm25_orIE/p1_bm25_orIE is the pair the task review named explicitly.
    assert B.VARIANTS["bm25_orIE"]["knobs"]["query_prefix"] == S.BARE
    assert B.VARIANTS["p1_bm25_orIE"]["knobs"]["query_prefix"] == S.PREFIX_P1
    instructed = {S.PREFIX_P1, S.PREFIX_P2, S.PREFIX_P3}
    for name, v in B.VARIANTS.items():
        if name == "baseline":
            continue   # {} -- production defaults, i.e. the instructed KNOB_DEFAULTS default
        qp = v["knobs"]["query_prefix"]
        assert qp == S.BARE or qp in instructed, name


def test_validate_knobs_rejects_typos_in_values_and_production_constants():
    for bad in ({"importance": "no-access"}, {"fts_score": "BM25"}, {"leg_merge": "maximum"},
                {"stub_guard": "Always"}, {"fts_mode": "or_if_few"}):
        with pytest.raises(ValueError, match="invalid knob values"):
            S.validate_knobs(bad)
    with pytest.raises(ValueError, match="rrfk"):                  # points at the replica twin
        S.validate_knobs({"RRF_K": 30})
    S.validate_knobs({"lexnorm": None, "stub_guard": None, "importance": "off"})


def test_imp_expr_variants_are_explicit():
    prod = S._imp_expr("prod")
    assert S._imp_expr("no_access") != prod and "importance_modifiers" in S._imp_expr("no_access")
    assert S._imp_expr("off") == "1.0" and S._imp_expr("no_decay") == "importance"
    with pytest.raises(ValueError):
        S._imp_expr("no-access")


# ---------------------------------------------------------------- read-only proof
class _ProbePool:
    """A pool whose UPDATE probe raises `exc` (None: the UPDATE is accepted)."""
    def __init__(self, exc, mode="on"):
        self.exc, self.mode = exc, mode

    @contextmanager
    def connection(self):
        pool = self

        class _Conn:
            def execute(self, sql, params=None):
                if sql.startswith("SHOW"):
                    return _FakeCursor({"transaction_read_only": pool.mode})
                if pool.exc is not None:
                    raise pool.exc
                return _FakeCursor(None)

            def rollback(self):
                pass
        yield _Conn()


def test_assert_readonly_accepts_only_the_read_only_refusal():
    ok = S.assert_readonly(SimpleNamespace(_pool=_ProbePool(
        psycopg.errors.ReadOnlySqlTransaction("cannot execute UPDATE in a read-only transaction"))))
    assert "read-only" in ok
    for exc in (psycopg.errors.UndefinedTable('relation "nodes" does not exist'),
                psycopg.errors.InsufficientPrivilege("permission denied"), None):
        with pytest.raises(S.CannotRun):
            S.assert_readonly(SimpleNamespace(_pool=_ProbePool(exc)))
    with pytest.raises(S.CannotRun, match="transaction_read_only"):
        S.assert_readonly(SimpleNamespace(_pool=_ProbePool(None, mode="off")))


# ---------------------------------------------------------------- traffic summary / loader
def _item(n_g2, hits, capped, ndcg, hub=0.5):
    return {"n_g2": n_g2, "n_g1": 0, "hits_k": hits, "hits_20": hits, "g1_hits_k": [],
            "capped": capped, "ndcg": ndcg, "hub_k": hub, "short_k": hub, "ms": 1.0}


def test_traffic_summary_leaves_items_without_gold_out_of_the_per_query_means():
    per = {"t1": _item(2, ["g"], 0.5, 0.6)}
    with_empty = {**per, "t2": _item(0, [], 0.0, 0.0, hub=0.0)}
    a, b = B.traffic_summary(per, None), B.traffic_summary(with_empty, None)
    for key in ("pooled_k", "capped_k", "mean_k", "ndcg", "hub", "short"):
        assert math.isclose(a[key], b[key]), key
    assert b["no_gold"] == ["t2"] and b["n"] == 2 and b["n_scored"] == 1


def test_load_traffic_gold_reports_items_it_skips(tmp_path, monkeypatch):
    (tmp_path / "retrieval").mkdir()
    base = {"trace_id": 1, "at": "2026-06-01T00:00:00+00:00", "query": "q", "args": {},
            "expected": {"memories": []}}
    rows = [{**base, "id": "tg-1", "tool": "search_memories", "tier": "adjudicated"},
            {**base, "id": "tg-2", "tool": "search_graph", "tier": "adjudicated"},
            {**base, "id": "tg-3", "tool": "search_graph", "tier": "single-annotator"}]
    (tmp_path / "retrieval" / "traffic_gold.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setenv("KB_EVAL_DATA_DIR", str(tmp_path))
    skipped = []
    assert [it["id"] for it in S.load_traffic_gold("adjudicated", skipped=skipped)] == ["tg-1"]
    assert skipped == ["tg-2"]                                     # only the requested tier's


# ---------------------------------------------------------------- query instruction / confidence
def test_shadow_defaults_to_the_shipped_instruction():
    from kb_core.embedding import QUERY_INSTRUCTION
    assert S.KNOB_DEFAULTS["query_prefix"] == QUERY_INSTRUCTION
    assert S.PREFIX_P1 == QUERY_INSTRUCTION
    assert B.VARIANTS["bare"]["knobs"] == {"query_prefix": S.BARE}


def test_served_prefix_reads_the_trace_marker():
    from kb_core.embedding import QUERY_INSTRUCTION, QUERY_INSTRUCTION_VERSION
    assert S.served_prefix({"trace": None}) == S.BARE
    assert S.served_prefix({"trace": {"crawl_mode": "deterministic"}}) == S.BARE
    assert S.served_prefix({"trace": {"query_vector": QUERY_INSTRUCTION_VERSION}}) == QUERY_INSTRUCTION
    with pytest.raises(S.CannotRun):
        S.served_prefix({"id": 1, "trace": {"query_vector": "instructed-v99"}})


def test_derive_pair_is_the_house_rule():
    weak, strong = C.derive_pair(junk_a=[0.40, 0.509], junk_b=[0.45, 0.5095],
                                 gold_a=[0.60, 0.62, 0.64], gold_b=[0.61, 0.63, 0.65])
    assert weak == 0.513                    # ceil to 3 dp of max(junk) + 0.003
    assert strong == round(0.904 * 0.62, 3)  # the lower median


def test_level_mirrors_search_confidence():
    assert C.level(None, False, 0.5, 0.6) == "keyword"
    assert C.level(0.9, True, 0.5, 0.6) == "scoped"
    assert C.level(0.61, False, 0.5, 0.6) == "strong"
    assert C.level(0.55, False, 0.5, 0.6) == "weak"
    assert C.level(0.45, False, 0.5, 0.6) == "none"
