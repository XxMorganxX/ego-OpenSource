"""Read-time parser for evals/retrieval/results/*.json -- display aggregates ONLY.

The committed result files are the provenance record (every reported number comes
from a committed file); this module derives what the dashboard's Retrieval page shows and nothing
normative -- prediction verdicting stays in evals/retrieval/report.py. Files are
classified by SHAPE, never filename, so future benchmark runs land on the page
without code changes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from statistics import mean

# dashboard/api/evals_reader.py -> parents[2] = KB_Core, parents[3] = the repo root
_DEFAULT_DIR = Path(__file__).resolve().parents[3] / "evals" / "retrieval" / "results"


def results_dir() -> Path:
    return Path(os.environ.get("KB_EVALS_RESULTS_DIR", str(_DEFAULT_DIR)))


def read_evals(root: "Path | None" = None) -> dict:
    root = root if root is not None else results_dir()
    out: dict = {"reference_runs": [], "grids": [], "off_corpus": None,
                 "ablations": None, "warnings": []}
    if not root.is_dir():
        out["warnings"].append(f"results dir not found: {root}")
        return out
    cells: list[tuple[str, dict]] = []
    for p in sorted(root.rglob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            out["warnings"].append(f"{p.name}: {exc}")
            continue
        kind = _classify(data)
        if kind == "reference":
            out["reference_runs"].append(_reference(data))
        elif kind == "cell":
            if isinstance(data.get("cell"), str) and isinstance(data.get("stamp"), dict):
                cells.append((p.name, data))
            else:
                out["warnings"].append(f"{p.name}: malformed cell file")
        elif kind == "off_corpus":
            if out["off_corpus"] is not None:
                out["warnings"].append(f"{p.name}: replaces an earlier off_corpus file")
            out["off_corpus"] = _off_corpus(data)
        elif kind == "ablations":
            if out["ablations"] is not None:
                out["warnings"].append(f"{p.name}: replaces an earlier ablations file")
            out["ablations"] = {"sweeps": data.get("sweeps", [])}
        else:
            out["warnings"].append(f"{p.name}: unrecognized shape")
    out["reference_runs"].sort(key=lambda r: r["ran_at"] or "")
    out["grids"] = _grids(cells, out["warnings"])
    return out


def _classify(data) -> "str | None":
    if not isinstance(data, dict):
        return None
    if "overall_must_recall" in data:
        return "reference"
    if "cell" in data and isinstance(data.get("stamp"), dict):
        return "cell"
    if "probes" in data and "verdict" in data:
        return "off_corpus"
    if "sweeps" in data:
        return "ablations"
    return None


def _reference(data: dict) -> dict:
    return {k: data.get(k) for k in ("ran_at", "overall_must_recall", "mrr",
                                     "items_scored", "items_total", "by_archetype")}


def _off_corpus(data: dict) -> dict:
    return {"verdict": data.get("verdict"),
            "threshold": data.get("confidence_weak"),
            "worst_best_similarity": data.get("worst_mode_b_best_similarity"),
            "probes": data.get("probes", [])}


def _date(ran_at):
    # "20250315T120000Z" -> "2025-03-15"; non-string or short values pass through
    # unchanged (defensive: a corrupt file's stamp.ran_at may be null or absent).
    if not isinstance(ran_at, str) or len(ran_at) < 8:
        return ran_at
    return f"{ran_at[:4]}-{ran_at[4:6]}-{ran_at[6:8]}"


def _pct(vals: "list[float]", q: float) -> "float | None":
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return round(s[f] + (s[c] - s[f]) * (k - f), 1)


def _grids(cells: "list[tuple[str, dict]]", warnings: "list[str]") -> "list[dict]":
    """Group repeat files by benchmark day, then aggregate each cell over repeats."""
    by_day: dict[str, dict[str, list[dict]]] = {}
    for _name, data in cells:
        stamp = data["stamp"]
        day = _date(str(stamp.get("ran_at") or ""))
        by_day.setdefault(day, {}).setdefault(data["cell"], []).append(data)
    grids = []
    for day in sorted(by_day):
        cell_rows = []
        heads = set()
        for name in sorted(by_day[day]):
            try:
                repeats = by_day[day][name]
                for r in repeats:
                    if r["stamp"].get("git_head"):
                        heads.add(r["stamp"]["git_head"][:7])
                meta = repeats[0]["stamp"].get("cell", {})
                recalls, negs, latencies = [], [], []
                arch: dict[str, list[float]] = {}
                for r in repeats:
                    items = r.get("items", [])
                    scored = [it for it in items if it.get("must_recall") is not None]
                    if scored:
                        recalls.append(mean(it["must_recall"] for it in scored))
                    negs.append(sum(it.get("neg_hits") or 0 for it in items))
                    for it in items:
                        for call in it.get("calls", []):
                            if call.get("latency_ms") is not None:
                                latencies.append(call["latency_ms"])
                        if it.get("must_recall") is not None and it.get("archetype"):
                            arch.setdefault(it["archetype"], []).append(it["must_recall"])
                cell_rows.append({
                    "cell": name,
                    "set": meta.get("set"),
                    "strategy": meta.get("strategy"),
                    "judge": meta.get("judge"),
                    "repeats": len(repeats),
                    "must_recall": round(mean(recalls), 4) if recalls else None,
                    "neg_hits": round(mean(negs), 2) if negs else None,
                    "latency_p50_ms": _pct(latencies, 0.5),
                    "latency_p90_ms": _pct(latencies, 0.9),
                    "by_archetype": {a: round(mean(v), 4) for a, v in sorted(arch.items())},
                })
            except Exception as exc:
                warnings.append(f"grid {day}/{name}: {exc}")
        grids.append({"date": day, "git_heads": sorted(heads), "cells": cell_rows})
    return grids
