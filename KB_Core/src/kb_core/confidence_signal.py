"""Dimensionless confidence signals: how far a query's best hit stands out.

Pure -- no DB, no network. The bake-off harness (evals/retrieval/
shadow_signals.py) and production labelling (knowledge_base.search_confidence) call these
same functions, so the measured rule IS the shipped rule.

`curve` is a result set's similarities sorted DESCENDING. A background is the query's
cosines against a random reservoir of live notes, scored with the production ruler
(KnowledgeBase.similarity_for)."""
from __future__ import annotations

import bisect
import math
from collections.abc import Callable
from dataclasses import dataclass


def _mean(xs) -> float:
    return sum(xs) / len(xs)


def _std(xs) -> float:
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _median(xs) -> float:
    s = sorted(xs)
    k = len(s) // 2
    return s[k] if len(s) % 2 else (s[k - 1] + s[k]) / 2


@dataclass(frozen=True)
class BackgroundStats:
    values: tuple  # sorted ascending

    @classmethod
    def from_sims(cls, sims) -> "BackgroundStats | None":
        vals = tuple(sorted(float(s) for s in sims if s is not None))
        return cls(vals) if len(vals) >= 2 else None

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float:
        return _mean(self.values)

    @property
    def std(self) -> float:
        return _std(self.values)

    @property
    def max(self) -> float:
        return self.values[-1]

    def percentile_of(self, x: float) -> float:
        """Share of background values strictly below x."""
        return bisect.bisect_left(self.values, x) / self.n


@dataclass(frozen=True)
class Signal:
    family: str          # "background" | "shape"
    min_len: int         # shortest curve the signal is defined on
    fn: Callable


def _bg_z(c, bg):
    return None if bg.std == 0 else (c[0] - bg.mean) / bg.std


def _z_in(c, _):
    sd = _std(c)
    return None if sd == 0 else (c[0] - _mean(c)) / sd


SIGNALS: "dict[str, Signal]" = {
    "bg_z": Signal("background", 1, _bg_z),
    "bg_gap_max": Signal("background", 1, lambda c, bg: c[0] - bg.max),
    "bg_pct": Signal("background", 1, lambda c, bg: bg.percentile_of(c[0])),
    "gap12": Signal("shape", 2, lambda c, _: c[0] - c[1]),
    "gap_med": Signal("shape", 3, lambda c, _: c[0] - _median(c)),
    "z_in": Signal("shape", 3, _z_in),
    "sigma": Signal("shape", 3, lambda c, _: _std(c)),
}


def signal_value(name: str, curve: "list[float]", bg: "BackgroundStats | None") -> "float | None":
    s = SIGNALS[name]
    if len(curve) < s.min_len or (s.family == "background" and bg is None):
        return None
    return s.fn(curve, bg)


def level_from_signal(value: float, cut_weak: float, cut_strong: float) -> str:
    return "strong" if value >= cut_strong else "weak" if value >= cut_weak else "none"


@dataclass(frozen=True)
class ShippedRule:
    signal: str
    cut_weak: float
    cut_strong: float
    min_curve: int
    gate_signal: "str | None" = None
    gate_cut: "float | None" = None
    reservoir_size: "int | None" = None


def apply_rule(rule: ShippedRule, curve: "list[float]",
               bg: "BackgroundStats | None") -> "tuple[str, float] | None":
    """(level, value) under `rule`, or None when it cannot be evaluated -- the caller then
    falls back to the static CONFIDENCE_* pair. A gate (combination rule) must ALSO clear
    its cut for the result to be above 'none'."""
    if len(curve) < rule.min_curve:
        return None
    v = signal_value(rule.signal, curve, bg)
    if v is None:
        return None
    level = level_from_signal(v, rule.cut_weak, rule.cut_strong)
    if rule.gate_signal is not None:
        g = signal_value(rule.gate_signal, curve, bg)
        if g is None:
            return None
        if g < rule.gate_cut:
            level = "none"
    return level, v


# The rule search_confidence applies. None = the static CONFIDENCE_* pair (today's
# behaviour); set in ONE switch-over commit from the bake-off winner.
SHIPPED_RULE: "ShippedRule | None" = None
