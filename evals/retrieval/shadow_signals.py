#!/usr/bin/env python3
"""Confidence-signal bake-off -- write-free.

--collect   score every population query in BOTH modes (A = KnowledgeBase.search, B =
            cluster-crawl judge="none") on the read-only shadow pool with the shipped
            instructed vector; record each result curve, the production label, the
            bare (uninstructed) label (mode A), and background samples scored with the
            production ruler (similarity_for) over reservoirs of several sizes and seeds.
--evaluate  search every candidate signal's operating point (midpoint cuts, never an
            observed value) on the SELECTION sets, apply the pre-registered ship bar, check
            the HELD-OUT sets AND seed stability (none-vs-not-none identical across all 3
            seeds; strong<->weak flips tolerated and reported), rank qualifiers;
            background/combo candidates with no latency measurement for their size, or
            whose p90 exceeds budget, are filtered out BEFORE ranking (fail-closed), so a
            qualifying runner-up still wins.
--verify    after wiring: compare today's production labels against a chosen winner's
            per-query bake-off levels. A key missing on either side is a failure. Otherwise
            a mismatch is drift (the query's top-1 moved since the bake-off); else, for a
            'shape' winner any level difference is a rule mismatch, for a
            'background'/'combo' winner a strong<->weak difference is seed_tolerated
            (reported, not a failure) and anything touching 'none' is still a rule
            mismatch. Passes only with zero rule AND zero missing.

SELF-COMPARISON GUARD: a background reservoir that does not exclude the query's own
result ids can contain the query's own top hit, comparing it against itself.
collect() therefore runs the query FIRST (both modes) and scores every reservoir through
reservoir_minus_hits(ids, hit_ids), recording bg_excluded per record.

Run from evals/retrieval with KB_Retrieval's venv (it carries kb_core + kb_retrieval):
  KB_DATABASE_URL=... KB_EVAL_DATA_DIR=... KB_INFERENCE_URL=... \\
      ../../KB_Retrieval/.venv/bin/python shadow_signals.py --collect
Exit: 0 ok (evaluate: a winner exists), 1 evaluate found no winner / verify rule or
missing mismatch, 2 could not run (verify: the evaluation JSON has no winner)."""
import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone

import shadow as S
import shadow_confidence as SC
from kb_core.confidence_signal import SIGNALS, BackgroundStats, level_from_signal, signal_value

SELECTION = ("authored", "traffic_adj")
HELD_OUT = ("traffic_single", "package")
SIZES = (200, 300, 500)
SEEDS = ("s0", "s1", "s2")
MIN_CURVES = (2, 3, 5)
# Symmetric strong-count guard slack (a deliberate design decision): a
# candidate's strong count per gold set/mode may differ from the static pair's by at most
# max(1 record, this fraction of the static count) -- blocks both strong-label LOSS
# (the original intent) and INFLATION (a saturating signal like bg_pct=1.0 everywhere
# labelling everything strong with no weak band).
STRONG_SLACK = 0.10
BG_P90_BUDGET_MS = 20.0
SIMPLICITY = {"shape": 0, "background": 1, "combo": 2}
COMBO_SIZE = 300      # combinations use one reservoir size (bounds the search space)
GATE_Q = 10            # gate-cut subsample for combinations: at most this many quantile positions


# ------------------------------------------------------------------------- populations
def populations() -> list:
    """(set, id, query, args) for junk, authored, traffic (adjudicated / single), package."""
    out = [("junk", f"junk-{i}", p, {"limit": 20}) for i, p in enumerate(SC.load_probes())]
    for it, a, _ in S.authored_search_items(S.load_authored_gold()):
        a = dict(a)
        out.append(("authored", it["id"], a.pop("query"), a))
    for it in S.load_traffic_gold("all"):
        a = {k: v for k, v in it["args"].items() if k != "query"}
        out.append(("traffic_adj" if it["tier"] == "adjudicated" else "traffic_single",
                    it["id"], it["query"], a))
    pkg = S.load_jsonl(S.eval_data_dir() / "retrieval" / "golden_package.jsonl")
    for it in pkg:
        for i, step in enumerate(s for s in it.get("tool_plan") or [] if s.get("tool") == "search_memories"):
            a = dict(step.get("args") or {})
            out.append(("package", f"{it['id']}#{i}", a.pop("query", None) or it["request"], a))
    return out


# ----------------------------------------------------------------------------- collect
def _curve(rows) -> list:
    return sorted((float(r.similarity) for r in rows if r.similarity is not None), reverse=True)


def _prod_confidence(rows, scoped) -> dict:
    from kb_core.knowledge_base import search_confidence
    return search_confidence(rows, scoped=scoped)


def reservoir_minus_hits(ids: list, hit_ids: set) -> list:
    """Drop a query's own result ids (its search + crawl rows) from a background
    reservoir before scoring it -- a background must never compare a hit against itself.
    Pure; order-preserving; a no-op when nothing in `ids` is in `hit_ids`."""
    return [i for i in ids if i not in hit_ids]


def _corpus_notes(kb) -> int:
    """The live embedded note count, read-only through the shadow kb's pool -- recorded
    in the records JSON so a future re-test trigger can compare against it exactly, instead
    of a stamped constant that rots as the corpus grows."""
    with kb._pool.connection() as c:
        return int(c.execute("SELECT count(*) AS n FROM nodes WHERE type = 'note' "
                             "AND dead_at IS NULL AND embedding IS NOT NULL").fetchone()["n"])


def collect(kb, max_embeds: int) -> dict:
    from kb_retrieval.contract import Query
    from kb_retrieval.registry import get_strategy
    pops = populations()
    S.check_embed_budget([(q, p) for _, _, q, _ in pops for p in (S.QUERY_INSTRUCTION, S.BARE)], max_embeds)
    reservoirs = {f"{n}:{s}": kb.background_reservoir_ids(n, seed=s) for n in SIZES for s in SEEDS}
    crawl = get_strategy("cluster-crawl", kb, judge="none")
    corpus_notes = _corpus_notes(kb)
    records, timing = [], {n: [] for n in SIZES}
    for set_, id_, q, args in pops:
        kw, limit = S.norm_args(args)
        scoped = any(kw.get(k) is not None for k in SC.SCOPED_KEYS)
        inst, bare = S.embedder()(q, S.QUERY_INSTRUCTION), S.embedder()(q, S.BARE)
        # run the query's OWN searches first, so their result ids can be excluded from
        # every background reservoir below -- scoring a reservoir that still contains the
        # query's own top hit compares a hit against itself (see the knowledge_base.py
        # calibration note).
        rows_a = kb.search(q, embedding=inst, limit=limit, **kw)
        bare_rows = kb.search(q, embedding=bare, limit=limit, **kw)
        # the RAW max similarity of the bare rows, not search_confidence's 4-dp-rounded
        # best_similarity -- same empty/scoped/keyword precedence and 0.53/0.62 cuts.
        bare_sims = [r.similarity for r in bare_rows if r.similarity is not None]
        raw_best = max(bare_sims) if bare_sims else None
        bare_level = ("none" if not bare_rows else "scoped" if scoped else "keyword"
                      if raw_best is None else "strong" if raw_best >= 0.62
                      else "weak" if raw_best >= 0.53 else "none")
        res = crawl.search(Query(text=q, embedding=inst, label_embedding=bare, limit=limit, **kw))
        hit_ids = {r.node.id for r in rows_a} | {r.id for r in res.rows}
        bg, bg_excluded = {}, {}
        for key, ids in reservoirs.items():
            filtered = reservoir_minus_hits(ids, hit_ids)
            bg_excluded[key] = len(ids) - len(filtered)
            t0 = time.perf_counter()
            sims = kb.similarity_for(filtered, inst)
            timing[int(key.split(":")[0])].append((time.perf_counter() - t0) * 1000)
            # round to 6dp -- smaller files; verify_compare only ever reads curves.
            bg[key] = sorted(round(float(v), 6) for v in sims.values() if v is not None)
        records.append({"set": set_, "id": id_, "mode": "A", "n": len(rows_a), "scoped": scoped,
                        "curve": _curve(rows_a), "prod_level": _prod_confidence(rows_a, scoped)["level"],
                        "bare_level": bare_level, "bg": bg, "bg_excluded": bg_excluded})
        records.append({"set": set_, "id": id_, "mode": "B", "n": len(res.rows), "scoped": scoped,
                        "curve": _curve(res.rows), "prod_level": res.confidence["level"],
                        "bare_level": None, "bg": bg, "bg_excluded": bg_excluded})
    lat = {n: {"p50": statistics.median(v), "p90": sorted(v)[int(0.9 * (len(v) - 1))]} for n, v in timing.items()}
    return {"started_at": datetime.now(timezone.utc).isoformat(), "git": S.git_state(),
            "sizes": SIZES, "seeds": SEEDS, "latency_ms": lat, "corpus_notes": corpus_notes,
            "records": records}


# ---------------------------------------------------------------------------- evaluate
def _signal_pair(rec: dict, cand: dict) -> tuple:
    """(value, gate_value) for one record under a candidate; None where undefined."""
    if rec["n"] == 0 or rec["scoped"] or not rec["curve"] or len(rec["curve"]) < cand["min_curve"]:
        return None, None
    bg = BackgroundStats.from_sims(rec["bg"].get(cand["key"], [])) if cand.get("key") else None
    v = signal_value(cand["signal"], rec["curve"], bg)
    g = signal_value(cand["gate"], rec["curve"], bg) if cand.get("gate") else None
    return v, g


def _level(rec: dict, v, g, cand: dict, cut_w: float, cut_s: float, gate_cut) -> str:
    """Empty -> none; scoped / keyword untouched; signal (or gate) undefined -> the static
    production label; else the signal's level, forced to 'none' if a gate does not clear."""
    if rec["n"] == 0:
        return "none"
    if rec["scoped"]:
        return "scoped"
    if not rec["curve"]:
        return "keyword"
    if v is None or (cand.get("gate") and g is None):
        return rec["prod_level"]
    level = level_from_signal(v, cut_w, cut_s)
    if cand.get("gate") and g < gate_cut:
        level = "none"
    return level


def candidate_level(rec: dict, cand: dict, cut_w: float, cut_s: float, gate_cut=None) -> str:
    """The label a candidate gives one record (the single-record form of the evaluation)."""
    v, g = _signal_pair(rec, cand)
    return _level(rec, v, g, cand, cut_w, cut_s, gate_cut)


def counts(records: list, levels: list) -> dict:
    out = {}
    for r, lv in zip(records, levels):
        c = out.setdefault((r["set"], r["mode"]), {"none": 0, "strong": 0})
        c["none"] += lv == "none"
        c["strong"] += lv == "strong"
    return out


def qualifies(by: dict, base: dict, sets, junk_total: dict) -> bool:
    for mode, total in junk_total.items():
        if by.get(("junk", mode), {}).get("none", 0) != total:
            return False
    for (set_, mode), c in by.items():
        if set_ not in sets:
            continue
        b = base.get((set_, mode), {"none": 0, "strong": 0})
        if c["none"] > b["none"]:
            return False
        if abs(c["strong"] - b["strong"]) > max(1, STRONG_SLACK * b["strong"]):
            return False
    return True


def six(records: list) -> set:
    return {(r["set"], r["id"]) for r in records if r["mode"] == "A" and r["set"] in HELD_OUT
            and r["prod_level"] == "none" and r["bare_level"] not in (None, "none")}


def _auc(gold: list, junk: list) -> float:
    if not gold or not junk:
        return float("nan")
    wins = sum((g > j) + 0.5 * (g == j) for g in gold for j in junk)
    return wins / (len(gold) * len(junk))


def _auc_sort_key(auc: float) -> float:
    """A NaN AUC ranks LAST, deterministically (NaN compares False against everything,
    which would otherwise make sort order depend on the input's prior ordering)."""
    return float("inf") if math.isnan(auc) else -auc


def _candidates() -> list:
    out = []
    shape = [n for n, s in SIGNALS.items() if s.family == "shape"]
    bgs = [n for n, s in SIGNALS.items() if s.family == "background"]
    for name in shape:
        # a candidate whose min_curve is below the signal's own min_len is a duplicate
        # of the min_len variant (both score curves shorter than min_len as "undefined"),
        # so only generate min_curve values >= min_len.
        min_len = SIGNALS[name].min_len
        out += [{"signal": name, "family": "shape", "min_curve": m, "key": None}
                for m in MIN_CURVES if m >= min_len]
    for name in bgs:
        out += [{"signal": name, "family": "background", "min_curve": 1, "key": f"{n}:s0", "size": n}
                for n in SIZES]
    for b in bgs:          # combinations: background signal sets the level, a shape gate must clear
        for sh in shape:
            out.append({"signal": b, "family": "combo", "min_curve": 3, "key": f"{COMBO_SIZE}:s0",
                        "size": COMBO_SIZE, "gate": sh})
    return out


# ------------------------------------------------------------------- midpoint cut search
def _midpoints(values) -> list:
    """Candidate cuts: midpoints between adjacent DISTINCT sorted values, paired with the
    width of the gap each centres. Never an observed value itself: a cut sitting
    exactly on an observed value overfits the selection sample and can flip across seeds."""
    vs = sorted({v for v in values if v is not None})
    return [((vs[i] + vs[i + 1]) / 2.0, vs[i + 1] - vs[i]) for i in range(len(vs) - 1)]


def _subsample(seq: list, q: int) -> list:
    """Up to `q` evenly spaced elements of `seq` by index (de-duplicated, order preserved) --
    the gate-cut search space for combinations."""
    if len(seq) <= q:
        return seq
    idxs = sorted({min(len(seq) - 1, int(i * (len(seq) - 1) / (q - 1))) for i in range(q)})
    return [seq[i] for i in idxs]


def _none_only_qualifies(by: dict, base: dict, sets, junk_total: dict) -> bool:
    """Step 2's bar: junk 9/9 'none' in each mode, and no SELECTION gold set/mode gets more
    'none' labels than the static pair. The strong-label guard is step 3's job -- 'none'-ness
    does not depend on cut_strong, so this intentionally omits it."""
    for mode, total in junk_total.items():
        if by.get(("junk", mode), {}).get("none", 0) != total:
            return False
    for set_ in sets:
        for mode in ("A", "B"):
            c = by.get((set_, mode))
            if c is None:
                continue
            b = base.get((set_, mode), {"none": 0, "strong": 0})
            if c["none"] > b["none"]:
                return False
    return True


def _strong_guard(by: dict, base: dict, sets) -> bool:
    """The same symmetric guard as `qualifies()` (kept as a separate, SELECTION-only check
    because `_choose_cut_strong` needs it mid-search, before a full `by`/`junk_total` exist)."""
    for set_ in sets:
        for mode in ("A", "B"):
            c = by.get((set_, mode))
            if c is None:
                continue
            b = base.get((set_, mode), {"none": 0, "strong": 0})
            if abs(c["strong"] - b["strong"]) > max(1, STRONG_SLACK * b["strong"]):
                return False
    return True


def _choose_cut_weak(sel: list, pairs: list, mids: list, cand: dict, base: dict,
                     junk_total: dict, gate_cut) -> "dict | None":
    """Step 2: pick cut_weak among the midpoint candidates. Qualifying w's are ranked by
    minimum SELECTION gold 'none' count, ties broken by the WIDEST gap (the midpoint
    farthest from its neighbours), which centres the cut in that gap."""
    best = None
    for w, gap in mids:
        levels = [_level(r, v, g, cand, w, w, gate_cut) for r, (v, g) in zip(sel, pairs)]
        by = counts(sel, levels)
        if not _none_only_qualifies(by, base, SELECTION, junk_total):
            continue
        gold_none = sum(by.get((s_, m), {"none": 0}).get("none", 0) for s_ in SELECTION for m in ("A", "B"))
        key = (gold_none, -gap)
        if best is None or key < best["key"]:
            best = {"w": w, "gap": gap, "gold_none": gold_none, "key": key}
    return best


def _choose_cut_strong(sel: list, pairs: list, mids: list, cand: dict, base: dict,
                       w: float, gate_cut) -> "dict | None":
    """Step 3: pick cut_strong >= w. Qualifying s's (the symmetric strong-count guard holds on
    every SELECTION set/mode -- STRONG_SLACK bounds LOSS and INFLATION alike) are ranked by
    minimum total |candidate strong - static strong| over the SELECTION sets/modes -- so
    strong counts stay close to today's and a weak band survives -- ties broken toward the
    higher s."""
    best = None
    for s, _gap in mids:
        if s < w:
            continue
        levels = [_level(r, v, g, cand, w, s, gate_cut) for r, (v, g) in zip(sel, pairs)]
        by = counts(sel, levels)
        if not _strong_guard(by, base, SELECTION):
            continue
        dist = sum(abs(by.get((s_, m), {"strong": 0}).get("strong", 0)
                       - base.get((s_, m), {"strong": 0}).get("strong", 0))
                   for s_ in SELECTION for m in ("A", "B"))
        key = (dist, -s)
        if best is None or key < best["key"]:
            best = {"s": s, "dist": dist, "key": key}
    return best


def _best_point(records: list, cand: dict, base: dict, junk_total: dict) -> "dict | None":
    """One candidate's operating point on the SELECTION sets (authored, traffic_adj, junk):
    midpoint cut_weak (widest qualifying gap), then midpoint cut_strong (closest to the
    static strong counts), iterating the gate cut for combinations (subsampled to GATE_Q
    quantile positions, picked by the same keys: gold none, gap width, strong-distance).
    Signal/gate values are computed ONCE per record; every candidate cut is a cheap relabel."""
    sel = [r for r in records if r["set"] in SELECTION + ("junk",)]
    pairs = [_signal_pair(r, cand) for r in sel]
    mids = _midpoints(v for v, _ in pairs)
    if not mids:
        return None
    if cand.get("gate"):
        gate_mids = _subsample(_midpoints(g for _, g in pairs), GATE_Q)
        if not gate_mids:
            return None
    else:
        gate_mids = [(None, 0.0)]
    best = None
    for gate_cut, _ in gate_mids:
        w_pick = _choose_cut_weak(sel, pairs, mids, cand, base, junk_total, gate_cut)
        if w_pick is None:
            continue
        s_pick = _choose_cut_strong(sel, pairs, mids, cand, base, w_pick["w"], gate_cut)
        if s_pick is None:
            continue
        key = (w_pick["gold_none"], -w_pick["gap"], s_pick["dist"])
        if best is None or key < best["key"]:
            best = {"cut_weak": w_pick["w"], "cut_strong": s_pick["s"], "gate_cut": gate_cut, "key": key}
    return best


def _p90_for_size(latency_ms, size) -> "float | None":
    """`size` -> its p90, accepting both int and str keys (JSON round-trips turn dict keys
    into strings); None when there is no latency data for it."""
    if not latency_ms:
        return None
    for key in (size, str(size)):
        entry = latency_ms.get(key)
        if entry is not None:
            return entry.get("p90")
    return None


def _seed_stability(records: list, cand: dict, pt: dict, base: dict, junk_total: dict,
                    s0_levels: list) -> tuple:
    """Seed-stability rule (a deliberate design decision): a background/combo candidate is
    seed_stable iff, at its chosen cuts, EVERY seed (s0, s1, s2) gives (a) a none-vs-not-none
    outcome identical to s0's for every record -- strong<->weak differences are TOLERATED,
    none<->weak and none<->strong are NOT -- and (b) the full ship bar holds: `qualifies()`
    on the selection sets (including junk) AND the held-out check. Returns (stable,
    strong_weak_flips, reason), counting s1/s2 records that tolerably differ from s0.

    `reason` names the FIRST thing that broke stability (or "ok"): "none-flip" for (a),
    "bar-fails-at-seed:<seed>" for (b) -- a none-flip is reported first because the rule
    tolerates nothing about it, whereas a bar failure is at least seed-localized."""
    stable, flips, reason = True, 0, "ok"
    for seed in SEEDS:
        if seed == SEEDS[0]:
            seed_levels = s0_levels
        else:
            alt = dict(cand, key=f"{cand['size']}:{seed}")
            seed_levels = [candidate_level(r, alt, pt["cut_weak"], pt["cut_strong"], pt["gate_cut"])
                          for r in records]
            for base_lv, lv in zip(s0_levels, seed_levels):
                if (base_lv == "none") != (lv == "none"):
                    stable = False
                    if reason == "ok":
                        reason = "none-flip"
                elif base_lv != lv:
                    flips += 1     # the only remaining difference possible is strong<->weak
        by = counts(records, seed_levels)
        if not qualifies(by, base, SELECTION, junk_total) or not qualifies(by, base, HELD_OUT, junk_total={}):
            stable = False
            if reason == "ok":
                reason = f"bar-fails-at-seed:{seed}"
    return stable, flips, reason


def _junk_max(records: list, cand: dict) -> dict:
    """Diagnostic: the maximum signal value among junk records, per mode -- independent
    of any cut, so a non-qualifying candidate's table row still says how close it got.
    A mode with no defined junk value (e.g. every junk curve shorter than cand's min_curve)
    reports None for that mode."""
    out = {}
    for mode in ("A", "B"):
        vals = [v for r in records if r["set"] == "junk" and r["mode"] == mode
                for v, _ in (_signal_pair(r, cand),) if v is not None]
        out[mode] = max(vals) if vals else None
    return out


def _min_new_none_at_junk_clean_cut(records: list, cand: dict, base: dict, junk_total: dict) -> "float | None":
    """Diagnostic for a candidate with NO qualifying operating point (`_best_point`
    returned None): among every midpoint cut_weak (and, for a combo, every subsampled gate
    cut) that still keeps junk 9/9 'none' in each mode -- ignoring the SELECTION none-count
    ceiling and the strong-count guard, which are `_best_point`'s job, not this one's -- the
    minimum number of SELECTION-set gold records that flip to 'none' beyond what the static
    pair already calls 'none' there. None when no midpoint keeps junk fully clean at all
    (the candidate cannot even satisfy the junk bar alone)."""
    sel = [r for r in records if r["set"] in SELECTION + ("junk",)]
    pairs = [_signal_pair(r, cand) for r in sel]
    mids = _midpoints(v for v, _ in pairs)
    if not mids:
        return None
    gate_mids = _subsample(_midpoints(g for _, g in pairs), GATE_Q) if cand.get("gate") else [(None, 0.0)]
    best = None
    for gate_cut, _ in gate_mids:
        for w, _gap in mids:
            levels = [_level(r, v, g, cand, w, w, gate_cut) for r, (v, g) in zip(sel, pairs)]
            by = counts(sel, levels)
            if any(by.get(("junk", m), {"none": 0}).get("none", 0) != t for m, t in junk_total.items()):
                continue
            extra = sum(max(0, by.get((s_, m), {"none": 0}).get("none", 0)
                           - base.get((s_, m), {"none": 0}).get("none", 0))
                       for s_ in SELECTION for m in ("A", "B"))
            if best is None or extra < best:
                best = extra
    return best


def _abs_top1_reference(records: list) -> dict:
    """A reference row, `signal="abs_top1"` / `family="reference"` -- the absolute top-1
    cosine (today's shipped rule, no cut search at all) as the yardstick every candidate's
    AUC/junk_max is read against. It carries no `held_out_pass`/`seed_stable` keys, so the
    winners filter in `select()` excludes it structurally: it can never win, not merely lose
    on comparison."""
    gold_v = [r["curve"][0] for r in records if r["set"] != "junk" and r["curve"]]
    junk_v = [r["curve"][0] for r in records if r["set"] == "junk" and r["curve"]]
    junk_max = {m: max((r["curve"][0] for r in records
                        if r["set"] == "junk" and r["mode"] == m and r["curve"]), default=None)
               for m in ("A", "B")}
    return {"signal": "abs_top1", "family": "reference", "qualified": False,
            "auc": _auc(gold_v, junk_v), "junk_max": junk_max, "seed_stable_reason": "n/a"}


def select(records: list, latency_ms: "dict | None" = None) -> dict:
    base = counts(records, [r["prod_level"] for r in records])
    sixes = six(records)
    sel = [r for r in records if r["set"] in SELECTION + ("junk",)]
    junk_total = {m: sum(1 for r in sel if r["set"] == "junk" and r["mode"] == m) for m in ("A", "B")}
    # junk missing entirely (junk_total == 0) must not pass silently -- it would make
    # every candidate trivially "qualify" on the junk bar.
    expected_junk = len(SC.load_probes())
    for mode in ("A", "B"):
        if junk_total.get(mode, 0) != expected_junk:
            raise S.CannotRun(f"expected {expected_junk} junk records in mode {mode!r}, found "
                              f"{junk_total.get(mode, 0)} -- the collected records are missing junk probes")
    table = []
    for cand in _candidates():
        pairs = [_signal_pair(r, cand) for r in records]
        gold_v = [v for r, (v, _) in zip(records, pairs) if r["set"] != "junk" and v is not None]
        junk_v = [v for r, (v, _) in zip(records, pairs) if r["set"] == "junk" and v is not None]
        auc = _auc(gold_v, junk_v)
        base_fields = {k: v for k, v in cand.items() if k != "key"}
        pt = _best_point(records, cand, base, junk_total)
        if pt is None:
            # a stopped candidate still gets a table row -- diagnostics, not silence.
            table.append({**base_fields, "qualified": False, "auc": auc,
                          "junk_max": _junk_max(records, cand),
                          "min_new_none_at_junk_clean_cut":
                              _min_new_none_at_junk_clean_cut(records, cand, base, junk_total),
                          "seed_stable_reason": "n/a"})
            continue
        levels = [_level(r, v, g, cand, pt["cut_weak"], pt["cut_strong"], pt["gate_cut"])
                  for r, (v, g) in zip(records, pairs)]
        by = counts(records, levels)
        held = qualifies(by, base, HELD_OUT, junk_total={})
        if cand["family"] == "shape":
            # Shape signals have no reservoir seed at all -- "stable across seeds" does not
            # apply, so this is reported "n/a", not a checked-and-passing "ok".
            stable, strong_weak_flips, stable_reason = True, 0, "n/a"
        else:
            stable, strong_weak_flips, stable_reason = _seed_stability(records, cand, pt, base, junk_total, levels)
        # Fail-closed by design: a background/combo candidate with
        # NO latency measurement for its size is rejected too -- missing data is never
        # "within budget". Filtered HERE, before ranking, so a qualifying runner-up still
        # wins instead of the run stopping cold.
        latency_rejected, latency_reason = False, None
        if cand["family"] != "shape":
            p90 = _p90_for_size(latency_ms, cand["size"])
            if p90 is None:
                latency_rejected, latency_reason = True, "no measurement"
            elif p90 > BG_P90_BUDGET_MS:
                latency_rejected, latency_reason = True, "over budget"
        rescued = sum(1 for r, lv in zip(records, levels)
                      if r["mode"] == "A" and (r["set"], r["id"]) in sixes and lv != "none")
        entry = {**base_fields, "qualified": True, "cut_weak": pt["cut_weak"],
                 "cut_strong": pt["cut_strong"], "gate_cut": pt["gate_cut"],
                 "held_out_pass": held, "seed_stable": stable, "seed_stable_reason": stable_reason,
                 "strong_weak_flips": strong_weak_flips,
                 "latency_rejected": latency_rejected, "latency_reason": latency_reason,
                 "rescued": rescued, "of_six": len(sixes), "auc": auc,
                 "counts": {f"{k[0]}:{k[1]}": v for k, v in by.items()}}
        entry["_levels"] = levels    # stripped below; kept only on the eventual winner
        table.append(entry)
    table.append(_abs_top1_reference(records))     # never eligible -- see its docstring
    winners = [t for t in table if t.get("held_out_pass") and t.get("seed_stable") and not t.get("latency_rejected")]
    winners.sort(key=lambda t: (-t["rescued"], _auc_sort_key(t["auc"]), SIMPLICITY[t["family"]]))
    winner = winners[0] if winners else None
    if winner is not None:
        wl = winner.pop("_levels")
        # the winner's per-query levels and top-1s, so --verify can compare per query
        # instead of aggregate counts, and tell drift (the query's top-1 moved) from rule
        # (the labelling rule disagrees).
        winner["levels"] = {f"{r['set']}|{r['id']}|{r['mode']}": lv for r, lv in zip(records, wl)}
        winner["top1"] = {f"{r['set']}|{r['id']}|{r['mode']}": (r["curve"][0] if r["curve"] else None)
                          for r in records}
    for t in table:
        t.pop("_levels", None)
    return {"baseline": {f"{k[0]}:{k[1]}": v for k, v in base.items()},
            "six": sorted(map(list, sixes)), "table": table, "winner": winner}


# ----------------------------------------------------------------------------- verify
def verify_compare(records: list, winner: dict) -> dict:
    """Per-query comparison of `records`' CURRENT prod_level against a bake-off winner's
    recorded per-query levels (`winner["levels"]` / `winner["top1"]`, written by `select()`).
    Pure and None-safe.

    Comparison rules (a deliberate design decision):
      (a) a key on EITHER side only (the winner saw a query the replay doesn't, or vice
          versa) is a 'missing' failure -- the verify gate must replay the exact population;
      (b) drift (the query's top-1 moved by more than 1e-6 since the bake-off) always wins
          over a level mismatch -- it means today's production label is stale evidence, not
          a rule disagreement;
      (c) otherwise, for a 'shape' winner (deterministic: the same curve always gives the
          same label) ANY level difference is a 'rule' mismatch; for a 'background'/'combo'
          winner (production reservoir seed differs from the bake-off's), a strong<->weak
          difference is 'seed_tolerated' (reported, does not fail) and anything touching
          'none' is still 'rule'.
    Exit (the verify gate) passes only with zero 'rule' AND zero 'missing'."""
    want_levels = winner.get("levels") or {}
    want_top1 = winner.get("top1") or {}
    deterministic = winner.get("family") == "shape"
    rec_by_key = {f"{r['set']}|{r['id']}|{r['mode']}": r for r in records}
    matched, drift, rule, seed_tolerated, missing = 0, [], [], [], []
    for key in sorted(set(want_levels) | set(rec_by_key)):
        if key not in rec_by_key or key not in want_levels:
            missing.append(key)
            continue
        r = rec_by_key[key]
        now_level, want_level = r["prod_level"], want_levels[key]
        if now_level == want_level:
            matched += 1
            continue
        now_top1 = r["curve"][0] if r["curve"] else None
        was_top1 = want_top1.get(key)
        row = {"key": key, "now": now_level, "want": want_level, "now_top1": now_top1, "was_top1": was_top1}
        if (now_top1 is None) != (was_top1 is None) or (
                now_top1 is not None and was_top1 is not None and abs(now_top1 - was_top1) > 1e-6):
            drift.append(row)
            continue
        if not deterministic and {now_level, want_level} == {"strong", "weak"}:
            seed_tolerated.append(row)
        else:
            rule.append(row)
    return {"matched": matched, "drift": drift, "rule": rule, "seed_tolerated": seed_tolerated,
            "missing": missing}


# ---------------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    return S.run_cli("shadow_signals", _main, argv)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--collect", action="store_true")
    g.add_argument("--evaluate", metavar="RECORDS_JSON")
    g.add_argument("--verify", nargs=2, metavar=("RECORDS_JSON", "EVALUATION_JSON"))
    ap.add_argument("--max-embeds", type=int, default=60)
    a = ap.parse_args(argv)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    S.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if a.collect:
        kb = S.kb()
        print("readonly:", S.assert_readonly(kb))
        S.embedder().max_calls = a.max_embeds
        data = collect(kb, a.max_embeds)
        path = S.RESULTS_DIR / f"signals-records-{stamp}.json"
        path.write_text(json.dumps(data))
        print(f"records: {len(data['records'])} | latency_ms {data['latency_ms']} | wrote {path.relative_to(S.HERE)}")
        return 0
    if a.evaluate:
        data = json.load(open(a.evaluate))
        ev = select(data["records"], latency_ms=data.get("latency_ms"))
        ev["latency_ms"] = data.get("latency_ms")
        path = S.RESULTS_DIR / f"signals-eval-{stamp}.json"
        path.write_text(json.dumps(ev, indent=1, default=str))
        # every candidate gets a row now, qualifying or not -- print the qualifying ones
        # first (as before), then the non-qualifying/reference diagnostics after.
        qualifying = [t for t in ev["table"] if t.get("qualified")]
        other = [t for t in ev["table"] if not t.get("qualified")]
        for t in sorted(qualifying, key=lambda t: (not t["held_out_pass"], -t["rescued"], _auc_sort_key(t["auc"]))):
            print(f"{t['signal']:11s} {t['family']:10s} size={t.get('size','-')!s:4s} mc={t['min_curve']} "
                  f"cuts=({t['cut_weak']:.4f},{t['cut_strong']:.4f}) held={t['held_out_pass']} "
                  f"stable={t['seed_stable']}({t['seed_stable_reason']}) flips={t['strong_weak_flips']} "
                  f"latency_rejected={t['latency_rejected']}({t['latency_reason']}) "
                  f"rescued={t['rescued']}/{t['of_six']} auc={t['auc']:.3f}")
        for t in sorted(other, key=lambda t: _auc_sort_key(t["auc"])):
            jm = t.get("junk_max") or {}
            jm_s = " ".join(f"{m}={v:.3f}" if v is not None else f"{m}=-" for m, v in jm.items())
            print(f"{t['signal']:11s} {t['family']:10s} size={t.get('size','-')!s:4s} mc={t.get('min_curve','-')!s:4s} "
                  f"qualified=False junk_max=({jm_s}) "
                  f"min_new_none_at_junk_clean_cut={t.get('min_new_none_at_junk_clean_cut')} auc={t['auc']:.3f}")
        print("WINNER:", json.dumps(ev["winner"], default=str) if ev["winner"] else "none -- stop condition")
        print(f"wrote {path.relative_to(S.HERE)}")
        return 0 if ev["winner"] else 1
    data, ev = json.load(open(a.verify[0])), json.load(open(a.verify[1]))
    winner = ev.get("winner")
    if not winner:
        print("VERIFY: could not run -- the evaluation JSON has no winner (no code was shipped)")
        return 2
    cmp = verify_compare(data["records"], winner)
    print(f"VERIFY: matched={cmp['matched']} drift={len(cmp['drift'])} rule={len(cmp['rule'])} "
          f"seed_tolerated={len(cmp['seed_tolerated'])} missing={len(cmp['missing'])}")
    if cmp["drift"]:
        print("  drift:", cmp["drift"])
    if cmp["seed_tolerated"]:
        print("  seed_tolerated:", cmp["seed_tolerated"])
    if cmp["rule"]:
        print("  rule:", cmp["rule"])
    if cmp["missing"]:
        print("  missing:", cmp["missing"])
    return 0 if not cmp["rule"] and not cmp["missing"] else 1


if __name__ == "__main__":
    sys.exit(main())
