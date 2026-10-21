"""Importance amplifier: modifier registry, aggregation, and real-time decay. Pure, no DB.

Every policy number — retrieval-recency floor / half-life, access saturation, the
project-dormancy half-life — is an argument or comes from the Policy the registry is
built from. Only the clamp constants are module-level."""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from kb_core.policy import DEFAULT_POLICY, Policy

IMPORTANCE_MIN = 0.1
IMPORTANCE_MAX = 5.0
IMPORTANCE_BASE = 1.0
_LN2 = math.log(2)


@dataclass
class NodeStats:
    access_count: int
    last_accessed_at: datetime | None
    ingested_at: datetime
    modifiers: dict = field(default_factory=dict)
    # Whole days since the node's project last changed; None = not a project doc, or no
    # activity signal at all (both mean "no decay"). Injected fresh at recompute time.
    project_dormant_days: int | None = None


@dataclass(frozen=True)
class Modifier:
    name: str
    coefficient: float
    compute: Callable[[NodeStats], float]


class ModifierRegistry:
    def __init__(self) -> None:
        self._mods: dict[str, Modifier] = {}

    def register(self, name: str, coefficient: float,
                 compute: Callable[[NodeStats], float]) -> None:
        self._mods[name] = Modifier(name, coefficient, compute)

    def modifiers(self) -> list[Modifier]:
        return list(self._mods.values())


def dormancy_factor(days: "int | float | None", half_life_days: float) -> float:
    """Fraction of the inherited boost a project doc keeps: 1.0 at 0 days (and for no
    signal), 0.5 at one half-life, no floor. Negative ages (future activity) clamp to 1.0."""
    if days is None:
        return 1.0
    return 2.0 ** (-max(0.0, float(days)) / half_life_days)


def default_modifier_registry(policy: Policy | None = None) -> ModifierRegistry:
    """The four modifiers, parameterized by the policy. `project_dormancy` is registered
    even without a [projects] section (it is then always 0.0) so the stored modifier map
    keeps one shape across policy edits — no recompute churn from adding the section."""
    policy = policy or DEFAULT_POLICY
    saturation = policy.importance.access_saturation
    # No [projects] section = no project key is ever computed (project_key_sql), so
    # project_dormant_days is always None in real recompute traffic. Hard-zero the
    # modifier rather than trust that alone: it must stay inert even if a caller passes
    # an explicit project_dormant_days without a policy to interpret it under.
    half_life = policy.projects.dormancy_half_life_days if policy.projects else None
    r = ModifierRegistry()
    r.register("manual", 1.0, lambda s: float(s.modifiers.get("manual", 0.0)))
    r.register("access", 0.5, lambda s: s.access_count / (s.access_count + saturation))
    # Cascading directory boost: the summed `manual` of a node's ancestor folders, injected
    # fresh from the DB at recompute time (see KnowledgeBase._recompute_in_conn).
    r.register("inherited", 1.0, lambda s: float(s.modifiers.get("inherited", 0.0)))

    # Project dormancy: the slice of `inherited` a quiet project has lost. Additive with a
    # negative coefficient so the registry stays a plain sum and the dashboard's factor
    # list explains it as its own line; total = 1 + manual + 0.5*access + inherited*factor.
    def _project_dormancy(s: NodeStats) -> float:
        if half_life is None:
            return 0.0
        return (float(s.modifiers.get("inherited", 0.0))
                * (1.0 - dormancy_factor(s.project_dormant_days, half_life)))

    r.register("project_dormancy", -1.0, _project_dormancy)
    return r


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_importance(stats: NodeStats, registry: ModifierRegistry) -> tuple[float, dict]:
    """Aggregate the registry's modifiers into (importance, raw-modifier-map)."""
    modifiers: dict[str, float] = {}
    total = IMPORTANCE_BASE
    for m in registry.modifiers():
        raw = m.compute(stats)
        modifiers[m.name] = raw
        total += m.coefficient * raw
    return _clamp(total, IMPORTANCE_MIN, IMPORTANCE_MAX), modifiers


def decay_factor(age_seconds: float, floor: float, half_life_seconds: float) -> float:
    """Exponential decay with a floor: 1.0 at age 0, asymptotes to `floor`."""
    return floor + (1 - floor) * math.exp(-_LN2 * age_seconds / half_life_seconds)


def effective_importance(importance: float, age_seconds: float, floor: float,
                         half_life_seconds: float) -> float:
    """Decay the boost above baseline; a neutral node (1.0) is never penalized."""
    return 1.0 + (importance - 1.0) * decay_factor(age_seconds, floor, half_life_seconds)
