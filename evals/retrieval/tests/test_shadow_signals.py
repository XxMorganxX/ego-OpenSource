"""Pure tests for the bake-off's evaluation and selection (no DB, no embeds)."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shadow_signals as G  # noqa: E402


def _rec(set_, id_, mode, curve, prod, scoped=False, n=None, bg=None, bare=None):
    return {"set": set_, "id": id_, "mode": mode, "curve": curve, "n": len(curve) if n is None else n,
            "scoped": scoped, "prod_level": prod, "bare_level": bare, "bg": bg or {}}


def test_candidate_level_respects_empty_scoped_keyword_and_fallback():
    cand = {"signal": "gap12", "family": "shape", "min_curve": 2, "key": None}
    assert G.candidate_level(_rec("authored", "a", "A", [], "none", n=0), cand, 0.05, 0.2) == "none"
    assert G.candidate_level(_rec("authored", "a", "A", [0.5, 0.1], "scoped", scoped=True), cand, 0.05, 0.2) == "scoped"
    assert G.candidate_level(_rec("authored", "a", "A", [], "keyword", n=3), cand, 0.05, 0.2) == "keyword"
    assert G.candidate_level(_rec("authored", "a", "A", [0.5], "weak"), cand, 0.05, 0.2) == "weak"   # too short -> static
    assert G.candidate_level(_rec("authored", "a", "A", [0.5, 0.1], "none"), cand, 0.05, 0.2) == "strong"


def test_qualifies_enforces_junk_none_parity_and_strong_guard():
    base = {("authored", "A"): {"none": 1, "strong": 10}, ("junk", "A"): {"none": 9, "strong": 0}}
    ok = {("authored", "A"): {"none": 1, "strong": 9}, ("junk", "A"): {"none": 9, "strong": 0}}
    assert G.qualifies(ok, base, ["authored"], junk_total={"A": 9})
    worse_none = {("authored", "A"): {"none": 2, "strong": 10}, ("junk", "A"): {"none": 9, "strong": 0}}
    assert not G.qualifies(worse_none, base, ["authored"], junk_total={"A": 9})
    strong_loss = {("authored", "A"): {"none": 1, "strong": 8}, ("junk", "A"): {"none": 9, "strong": 0}}
    assert not G.qualifies(strong_loss, base, ["authored"], junk_total={"A": 9})   # 20% loss
    junk_leak = {("authored", "A"): {"none": 1, "strong": 10}, ("junk", "A"): {"none": 8, "strong": 0}}
    assert not G.qualifies(junk_leak, base, ["authored"], junk_total={"A": 9})


def test_six_are_mode_a_gold_none_now_but_not_before():
    recs = [_rec("traffic_single", "t1", "A", [0.4], "none", bare="weak"),
            _rec("traffic_single", "t2", "A", [0.4], "none", bare="none"),
            _rec("package", "p1", "A", [0.4], "none", bare="strong"),
            _rec("package", "p1", "B", [0.4], "none", bare=None),
            _rec("junk", "j1", "A", [0.4], "none", bare="weak")]
    assert G.six(recs) == {("traffic_single", "t1"), ("package", "p1")}


# ------------------------------------------------------------- synthetic selection fixtures
# Midpoint cuts centred in the widest qualifying gap,
# cut_strong chosen to keep the strong counts closest to the static pair (so a weak band
# survives), latency filtered inside select() with fall-through, and --verify per query
# (drift vs rule). These synthetic records never touch a DB or an embedder.

_BG300 = {
    "s0": [0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.64, 0.66, 0.68],
    "s1": [round(x + 0.001, 4) for x in (0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.64, 0.66, 0.68)],
    "s2": [round(x - 0.001, 4) for x in (0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.64, 0.66, 0.68)],
}
_JUNK_TOP1 = [0.40, 0.41, 0.42, 0.43, 0.44, 0.45, 0.46, 0.47, 0.48]   # 9, matching off_corpus.PROBES
_GOLD = [("authored", "a1", 0.90, "weak"), ("authored", "a2", 0.92, "strong"),
         ("authored", "a3", 0.94, "strong"), ("traffic_adj", "t1", 0.91, "weak"),
         ("traffic_adj", "t2", 0.95, "strong")]
_HELD = [("traffic_single", "hs1", 0.93, "strong"), ("package", "hp1", 0.92, "weak")]


def _syn(set_, id_, mode, curve, prod_level):
    return {"set": set_, "id": id_, "mode": mode, "n": 1, "scoped": False, "curve": list(curve),
            "prod_level": prod_level, "bare_level": None,
            "bg": {f"300:{s}": vals for s, vals in _BG300.items()}}


def _population_records(curve_fn):
    """curve_fn(set_, top1) -> the full curve for that record; both modes are identical
    copies (nothing in this fix distinguishes mode A from B for these fixtures)."""
    records = []
    for mode in ("A", "B"):
        for i, top1 in enumerate(_JUNK_TOP1):
            records.append(_syn("junk", f"junk-{i}", mode, curve_fn("junk", top1), "none"))
        for set_, id_, top1, lvl in _GOLD + _HELD:
            records.append(_syn(set_, id_, mode, curve_fn(set_, top1), lvl))
    return records


def _clean_background_records():
    """Only a background signal is computable here (curve length 1 is too short for every
    shape/combo candidate), and its value (top1 vs. the size-300 reservoir) cleanly
    separates junk (top1 0.40-0.48) from gold (top1 0.90-0.95, background max ~0.68-0.69)."""
    return _population_records(lambda set_, top1: [top1])


def _clean_background_and_shape_records():
    """Same background separation as above, PLUS a curve length of 2 so gap12 (top1-top2)
    also cleanly separates junk (gap 0.01) from gold (gap 0.30)."""
    def curve_fn(set_, top1):
        gap = 0.01 if set_ == "junk" else 0.30
        return [top1, top1 - gap]
    return _population_records(curve_fn)


def _cuts_by_candidate(table):
    # table now also carries non-qualifying diagnostic rows (no cut_weak/cut_strong)
    # and the abs_top1 reference row (no min_curve at all) -- cuts only ever exist on a
    # qualified row, so that is what this comparison is over.
    return {(t["signal"], t["family"], t.get("size"), t["min_curve"], t.get("gate")):
            (t["cut_weak"], t["cut_strong"]) for t in table if t.get("qualified")}


def test_background_signal_cleanly_separable_gets_a_midpoint_cut_with_a_weak_band():
    records = _clean_background_records()
    ev = G.select(records, latency_ms={300: {"p50": 2.0, "p90": 10.0}})   # within budget
    w = ev["winner"]
    assert w is not None
    assert w["family"] == "background"
    assert w["held_out_pass"] is True
    assert w["seed_stable"] is True
    key = f"{w['size']}:s0"
    junk_vs, gold_vs = [], []
    for r in records:
        if r["mode"] != "A" or r["set"] not in ("junk",) + G.SELECTION:
            continue
        bg = G.BackgroundStats.from_sims(r["bg"][key])
        v = G.signal_value(w["signal"], r["curve"], bg)
        (junk_vs if r["set"] == "junk" else gold_vs).append(v)
    # cut_weak sits strictly between the two populations, never on an observed value.
    assert max(junk_vs) < w["cut_weak"] < min(gold_vs)
    assert w["cut_weak"] not in junk_vs and w["cut_weak"] not in gold_vs
    # the static pair's gold labels include "weak" (a1, t1), so a weak band must survive.
    assert w["cut_strong"] > w["cut_weak"]


def test_held_out_mutation_never_moves_the_chosen_cuts():
    records = _clean_background_records()
    ev1 = G.select(records)
    mutated = [dict(r) for r in records]
    for r in mutated:
        if r["set"] in G.HELD_OUT:
            r["curve"] = [0.05]            # would even flip this query to 'none'
            r["prod_level"] = "none"
    ev2 = G.select(mutated)
    assert _cuts_by_candidate(ev1["table"]) == _cuts_by_candidate(ev2["table"])


def test_latency_rejected_background_falls_through_to_next_qualifier():
    records = _clean_background_and_shape_records()
    ev = G.select(records, latency_ms={300: {"p50": 5.0, "p90": 25.0}})   # over BG_P90_BUDGET_MS
    rejected = [t for t in ev["table"] if t["family"] == "background" and t.get("qualified")]
    assert rejected and all(t["latency_rejected"] for t in rejected)
    w = ev["winner"]
    assert w is not None
    assert w["family"] == "shape"
    assert w["latency_rejected"] is False


def test_missing_latency_entry_fails_closed():
    """Fail-closed latency gating: a background/combo candidate with NO latency
    measurement for its reservoir size is rejected too -- missing data is never 'within
    budget'. `_clean_background_records()` has no shape/combo-eligible curve (length 1),
    so with every background candidate fail-closed there is no winner at all."""
    records = _clean_background_records()
    ev = G.select(records)   # no latency_ms at all
    bg_entries = [t for t in ev["table"] if t["family"] == "background" and t.get("qualified")]
    assert bg_entries and all(t["latency_rejected"] and t["latency_reason"] == "no measurement"
                              for t in bg_entries)
    assert ev["winner"] is None


def test_nan_auc_ranks_last_deterministically():
    assert G._auc_sort_key(float("nan")) == float("inf")
    assert G._auc_sort_key(1.0) < G._auc_sort_key(0.5) < G._auc_sort_key(0.0) < G._auc_sort_key(float("nan"))


# --------------------------------------------- Seed stability, strong guard, verify
# Seed instability moved to cut_strong, the symmetric strong guard, the
# per-query --verify adaptation, and fail-closed latency.

def test_symmetric_strong_guard_rejects_inflation_and_accepts_one_record_deviation():
    # static strong=3 on a small set: slack = max(1, 0.10*3=0.3) = 1 record. The OLD
    # loss-only 10% rule required c >= 0.9*3 = 2.7, i.e. failed a 1-record loss (c=2) --
    # exactly the one-record case on a small set that the rule exists for.
    base = {("package", "A"): {"none": 0, "strong": 3}, ("junk", "A"): {"none": 9, "strong": 0}}
    one_record_loss = {("package", "A"): {"none": 0, "strong": 2}, ("junk", "A"): {"none": 9, "strong": 0}}
    assert G.qualifies(one_record_loss, base, ["package"], junk_total={"A": 9})
    two_record_loss = {("package", "A"): {"none": 0, "strong": 1}, ("junk", "A"): {"none": 9, "strong": 0}}
    assert not G.qualifies(two_record_loss, base, ["package"], junk_total={"A": 9})
    # a saturating signal (e.g. bg_pct = 1.0 for every gold record) labels everything
    # strong with no weak band -- the symmetric guard must reject the inflation too.
    inflated = {("package", "A"): {"none": 0, "strong": 16}, ("junk", "A"): {"none": 9, "strong": 0}}
    assert not G.qualifies(inflated, base, ["package"], junk_total={"A": 9})


def _gaussian_bg(mu: float, sd: float, n: int, rng_seeds: tuple) -> dict:
    """{'300:s0': [...], '300:s1': [...], '300:s2': [...]} -- INDEPENDENT Gaussian draws
    per seed (realistic per-record background noise per seed: independent
    reservoir-resampling draws, not a constant shift applied to one shared list). One
    `random.Random(rs)` instance per seed, reused across its own n draws -- NOT re-seeded
    every draw, which would make every draw the RNG's identical first sample."""
    out = {}
    for seed, rs in zip(G.SEEDS, rng_seeds):
        rng = random.Random(rs)
        out[f"300:{seed}"] = sorted(rng.gauss(mu, sd) for _ in range(n))
    return out


def _bg_z_per_seed(curve: list, bg: dict) -> dict:
    return {seed: G.signal_value("bg_z", curve, G.BackgroundStats.from_sims(bg[f"300:{seed}"]))
            for seed in G.SEEDS}


def test_seed_stability_tolerates_strong_weak_but_not_none_weak_flips():
    cand = {"signal": "bg_z", "family": "background", "min_curve": 1, "key": "300:s0", "size": 300}
    junk = [{"set": "junk", "id": f"junk-{i}", "mode": "A", "n": 1, "scoped": False, "curve": [0.40],
             "prod_level": "none", "bare_level": None,
             "bg": _gaussian_bg(0.50, 0.05, 30, (10 + i, 20 + i, 30 + i))} for i in range(2)]
    junk_total = {"A": 2}
    junk_vals = [v for j in junk for v in _bg_z_per_seed(j["curve"], j["bg"]).values()]

    # Case 1: the realistic per-seed noise straddles the UPPER cut (strong<->weak) only --
    # the lower cut sits safely below every seed's value, so none-vs-not-none never moves.
    probe1 = {"set": "probe", "id": "p1", "mode": "A", "n": 1, "scoped": False, "curve": [0.70],
              "prod_level": "weak", "bare_level": None, "bg": _gaussian_bg(0.50, 0.05, 30, (111, 222, 333))}
    v1 = _bg_z_per_seed(probe1["curve"], probe1["bg"])
    assert max(v1.values()) > min(v1.values())     # the seeds really differ -- not a constant shift
    records1 = junk + [probe1]
    base1 = G.counts(records1, [r["prod_level"] for r in records1])
    cut_weak1 = (max(junk_vals) + min(v1.values())) / 2.0
    cut_strong1 = (min(v1.values()) + max(v1.values())) / 2.0     # splits the probe's own seed spread
    pt1 = {"cut_weak": cut_weak1, "cut_strong": cut_strong1, "gate_cut": None}
    s0_levels1 = [G.candidate_level(r, cand, cut_weak1, cut_strong1, None) for r in records1]
    stable1, flips1, reason1 = G._seed_stability(records1, cand, pt1, base1, junk_total, s0_levels1)
    assert stable1 is True
    assert flips1 >= 1
    assert reason1 == "ok"

    # Case 2: the SAME kind of noise straddles the LOWER cut instead (none<->weak) --
    # disqualifying, never tolerated, regardless of how small the underlying value shift is.
    probe2 = {"set": "probe", "id": "p2", "mode": "A", "n": 1, "scoped": False, "curve": [0.70],
              "prod_level": "weak", "bare_level": None, "bg": _gaussian_bg(0.50, 0.05, 30, (444, 555, 666))}
    v2 = _bg_z_per_seed(probe2["curve"], probe2["bg"])
    assert max(v2.values()) > min(v2.values())
    records2 = junk + [probe2]
    base2 = G.counts(records2, [r["prod_level"] for r in records2])
    cut_weak2 = (min(v2.values()) + max(v2.values())) / 2.0       # splits the probe's own seed spread
    cut_strong2 = max(v2.values()) + 10.0                          # nothing is ever "strong"
    pt2 = {"cut_weak": cut_weak2, "cut_strong": cut_strong2, "gate_cut": None}
    s0_levels2 = [G.candidate_level(r, cand, cut_weak2, cut_strong2, None) for r in records2]
    stable2, _flips2, reason2 = G._seed_stability(records2, cand, pt2, base2, junk_total, s0_levels2)
    assert stable2 is False
    assert reason2 == "none-flip"


def test_verify_compare_drift_beats_level_mismatch():
    winner = {"family": "shape", "levels": {"authored|a1|A": "strong"}, "top1": {"authored|a1|A": 0.90}}
    records = [{"set": "authored", "id": "a1", "mode": "A", "curve": [0.95], "prod_level": "weak"}]
    cmp = G.verify_compare(records, winner)
    assert [r["key"] for r in cmp["drift"]] == ["authored|a1|A"]
    assert not cmp["rule"] and not cmp["seed_tolerated"] and not cmp["missing"]


def test_verify_compare_missing_keys_count_as_failure():
    winner = {"family": "shape", "levels": {"authored|a1|A": "strong", "authored|a2|A": "weak"},
              "top1": {"authored|a1|A": 0.90, "authored|a2|A": 0.80}}
    records = [{"set": "authored", "id": "a1", "mode": "A", "curve": [0.90], "prod_level": "strong"}]
    cmp = G.verify_compare(records, winner)
    assert cmp["matched"] == 1
    assert cmp["missing"] == ["authored|a2|A"]
    # the exit decision fails on a nonzero 'missing' count even with zero 'rule' mismatches.
    assert (0 if not cmp["rule"] and not cmp["missing"] else 1) == 1


def test_verify_compare_background_winner_tolerates_strong_weak_but_not_none_weak():
    winner = {"family": "background", "levels": {"authored|a1|A": "strong", "authored|a2|A": "weak"},
              "top1": {"authored|a1|A": 0.90, "authored|a2|A": 0.80}}
    records = [
        {"set": "authored", "id": "a1", "mode": "A", "curve": [0.90], "prod_level": "weak"},   # strong<->weak
        {"set": "authored", "id": "a2", "mode": "A", "curve": [0.80], "prod_level": "none"},   # weak<->none
    ]
    cmp = G.verify_compare(records, winner)
    assert [r["key"] for r in cmp["seed_tolerated"]] == ["authored|a1|A"]
    assert [r["key"] for r in cmp["rule"]] == ["authored|a2|A"]
    assert (0 if not cmp["rule"] and not cmp["missing"] else 1) == 1


def test_verify_compare_shape_winner_does_not_tolerate_strong_weak():
    winner = {"family": "shape", "levels": {"authored|a1|A": "strong"}, "top1": {"authored|a1|A": 0.90}}
    records = [{"set": "authored", "id": "a1", "mode": "A", "curve": [0.90], "prod_level": "weak"}]
    cmp = G.verify_compare(records, winner)
    assert not cmp["seed_tolerated"]
    assert [r["key"] for r in cmp["rule"]] == ["authored|a1|A"]


# --------------------------------------------------------- reservoir_minus_hits (pure)

def test_reservoir_minus_hits_drops_only_the_querys_own_ids():
    ids = ["a", "b", "c", "d"]
    assert G.reservoir_minus_hits(ids, {"b", "d"}) == ["a", "c"]
    assert G.reservoir_minus_hits(ids, set()) == ids
    assert G.reservoir_minus_hits(ids, {"z"}) == ids       # no overlap -> unchanged
    assert G.reservoir_minus_hits([], {"a"}) == []
    assert G.reservoir_minus_hits(ids, {"a", "b", "c", "d"}) == []   # every id was a hit


# --------------------------------------------------------------- diagnostics for every row

def test_nothing_qualifies_gives_one_diagnostic_row_per_candidate_plus_reference():
    """When every candidate's signal is undefined everywhere (curve too short for shape,
    no background data for background/combo), `_best_point` returns None for all of them --
    `select()` must still emit one row per candidate (plus the abs_top1 reference), never an
    empty table, and there must be no winner."""
    records = [_rec("junk", f"junk-{i}", m, [0.40], "none", bg={}) for m in ("A", "B") for i in range(9)]
    ev = G.select(records)
    assert ev["winner"] is None
    non_ref = [t for t in ev["table"] if t["family"] != "reference"]
    assert len(non_ref) == len(G._candidates())
    assert non_ref      # not empty -- the whole point
    for t in non_ref:
        assert t["qualified"] is False
        assert "auc" in t and "junk_max" in t and "min_new_none_at_junk_clean_cut" in t
        assert t["seed_stable_reason"] == "n/a"
    refs = [t for t in ev["table"] if t["family"] == "reference"]
    assert len(refs) == 1
    assert refs[0]["signal"] == "abs_top1"
    assert refs[0]["qualified"] is False


def test_reference_row_present_but_never_wins():
    records = _clean_background_records()
    ev = G.select(records, latency_ms={300: {"p50": 2.0, "p90": 10.0}})
    refs = [t for t in ev["table"] if t["family"] == "reference"]
    assert len(refs) == 1 and refs[0]["signal"] == "abs_top1"
    # structurally ineligible, not merely out-ranked: no held_out_pass/seed_stable keys at
    # all, so the winners filter's `.get(...)` excludes it regardless of its AUC.
    assert "held_out_pass" not in refs[0] and "seed_stable" not in refs[0]
    assert ev["winner"] is not None
    assert ev["winner"]["family"] != "reference"


# ------------------------------------------ Untested-parts fix: pin the deferred minors

def test_choose_cut_strong_rejects_pure_inflation_with_no_alternative():
    """Pins the symmetric guard INSIDE `_choose_cut_strong` itself, not just `qualifies()`
    (the existing guard test exercises `qualifies()` directly, a sibling helper with the
    identical formula, but never this function). With the only offered cut_strong
    candidate inflating every authored record to 'strong', a loss-only guard (c >=
    0.9*baseline, which inflation always clears) would happily return a pick; the
    symmetric guard must reject it and leave none."""
    sel = [{"set": "authored", "mode": "A", "n": 1, "scoped": False, "curve": [1.0]} for _ in range(3)]
    pairs = [(0.90, None), (0.95, None), (0.99, None)]
    mids = [(0.5, 0.1)]      # the one offered cut -- every value clears it -> all 3 'strong'
    base = {("authored", "A"): {"none": 0, "strong": 1}}   # slack = max(1, 0.1*1) = 1; |3-1|=2 > 1
    assert G._choose_cut_strong(sel, pairs, mids, {}, base, w=0.1, gate_cut=None) is None


def test_seed_stability_requires_full_bar_not_just_none_flips():
    """Part (b) of the seed-stability rule: seed_stable also requires the full ship bar
    (`qualifies()` on SELECTION + HELD_OUT) to hold at EVERY seed -- not merely that no
    record's none-vs-not-none status moved. Two HELD_OUT 'strong' records both drop to
    'weak' at seed s1 (no none-flip anywhere: 'weak' is still non-none), but losing both
    strong labels breaches the symmetric strong-count guard (loss of 2 > slack of 1). A
    regression that dropped part (b) (checking only none-flips, per part (a)) would call
    this stable; it must not be."""
    cand = {"signal": "bg_z", "family": "background", "min_curve": 1, "key": "300:s0", "size": 300}
    junk = [{"set": "junk", "id": f"junk-{i}", "mode": "A", "n": 1, "scoped": False, "curve": [0.40],
             "prod_level": "none", "bare_level": None,
             "bg": {"300:s0": [0.45, 0.55], "300:s1": [0.45, 0.55], "300:s2": [0.45, 0.55]}}
            for i in range(2)]
    junk_total = {"A": 2}

    # Same background shape for both strong records at s0/s2 (mean .50, std .05); at s1 the
    # reservoir itself has shifted (mean .60, std .05) -- a REALISTIC per-seed resample, not
    # a per-record fudge. z = (top1 - mean)/std: s0/s2 give 8.0/8.4 (strong, cut_strong=7.2);
    # s1 gives 6.0/6.4 (weak -- still comfortably above cut_weak, never 'none').
    bg_s0s2 = [0.45, 0.55]
    bg_s1 = [0.55, 0.65]
    strongs = [
        {"set": "package", "id": "p1", "mode": "A", "n": 1, "scoped": False, "curve": [0.90],
         "prod_level": "strong", "bare_level": None,
         "bg": {"300:s0": bg_s0s2, "300:s1": bg_s1, "300:s2": bg_s0s2}},
        {"set": "package", "id": "p2", "mode": "A", "n": 1, "scoped": False, "curve": [0.92],
         "prod_level": "strong", "bare_level": None,
         "bg": {"300:s0": bg_s0s2, "300:s1": bg_s1, "300:s2": bg_s0s2}},
    ]
    records = junk + strongs
    base = G.counts(records, [r["prod_level"] for r in records])   # package strong=2

    cut_weak, cut_strong = 1.0, 7.2
    v_s0 = [G.signal_value("bg_z", r["curve"], G.BackgroundStats.from_sims(r["bg"]["300:s0"])) for r in strongs]
    v_s1 = [G.signal_value("bg_z", r["curve"], G.BackgroundStats.from_sims(r["bg"]["300:s1"])) for r in strongs]
    assert min(v_s0) > cut_strong > max(v_s1) > cut_weak      # the arithmetic the comment claims

    pt = {"cut_weak": cut_weak, "cut_strong": cut_strong, "gate_cut": None}
    s0_levels = [G.candidate_level(r, cand, cut_weak, cut_strong, None) for r in records]
    assert s0_levels[-2:] == ["strong", "strong"]
    stable, flips, reason = G._seed_stability(records, cand, pt, base, junk_total, s0_levels)
    assert stable is False
    assert reason == "bar-fails-at-seed:s1"
    assert flips >= 2   # both are tolerated strong<->weak diffs at s1, never none-flips
