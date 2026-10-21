"""Content-age decay for the ANALYSIS layers: pure math + its SQL twin. No DB, no network.

age_factor() de-emphasizes old dated content in concept clustering and entity promotion
only — retrieval never sees it. WHICH content decays, and how fast, is the policy's
[[decay]] rule list: each rule scopes by tags / path_prefix / source and
carries its own half-life and floor; the first matching rule wins, in file order; a
memory no rule matches, or with no event_at, is exactly 1.0. Future event_at clamps to
age 0: the factor is a decay, never a boost. No rules ⇒ the constant 1.0.

Mirrors importance.py: the pure functions exist for tests and callers, and the SQL fragment
is parity-tested against them (tests/test_aging.py)."""
import math
from collections.abc import Sequence

from kb_core.policy import DecayRule

_LN2 = math.log(2)


def _predicate(i: int, tags: str, path: str, source: str, rule: DecayRule) -> str:
    """The rule's scope as SQL over the row's tags/path/source column expressions. Only
    the conditions the rule declares are emitted; they AND together. Positive predicates,
    so a NULL column falls through to the next arm instead of matching."""
    conds = []
    if rule.match.tags:
        conds.append(f"{tags} @> %(d{i}_tags)s::text[]")
    if rule.match.path_prefix is not None:
        conds.append(f"{path} <@ %(d{i}_path)s::ltree")
    if rule.match.source is not None:
        conds.append(f"{source} = %(d{i}_source)s")
    return " AND ".join(conds)


def _curve(i: int, col: str) -> str:
    return (f"(CASE WHEN {col} IS NULL THEN 1.0\n"
            f"           ELSE %(d{i}_floor)s + (1 - %(d{i}_floor)s) *\n"
            f"                exp(-%(age_ln2)s * GREATEST(0, EXTRACT(EPOCH FROM (now() - {col})))"
            f" / %(d{i}_hl_secs)s)\n"
            f"           END)")


def age_sql(col: str, tags: str, path: str, source: str,
            rules: Sequence[DecayRule]) -> str:
    """The age-factor SQL fragment for one row: {col} a timestamptz expression (e.g.
    "m.event_at"), {tags} the text[] tags column, {path} the ltree path column, {source}
    the source column. First matching rule wins. Callers merge age_params(rules, …) into
    their query parameters."""
    if not rules:
        return "1.0::float8"
    arms = "\n".join(f"      WHEN {_predicate(i, tags, path, source, r)} THEN {_curve(i, col)}"
                     for i, r in enumerate(rules))
    return f"(CASE\n{arms}\n      ELSE 1.0 END)"


def age_params(rules: Sequence[DecayRule], enabled: bool = True) -> dict:
    """Query parameters for the SQL twin. With enabled=False every floor rides as 1.0,
    which collapses each arm to exactly 1.0 — the OFF switch needs no SQL branching."""
    params: dict = {"age_ln2": _LN2}
    for i, r in enumerate(rules):
        params[f"d{i}_floor"] = 1.0 if not enabled else r.floor
        params[f"d{i}_hl_secs"] = r.half_life_days * 86400.0
        if r.match.tags:
            params[f"d{i}_tags"] = list(r.match.tags)
        if r.match.path_prefix is not None:
            params[f"d{i}_path"] = r.match.path_prefix
        if r.match.source is not None:
            params[f"d{i}_source"] = r.match.source
    return params


def matching_rule(rules: Sequence[DecayRule], *, tags: Sequence[str], path: str,
                  source: str) -> "DecayRule | None":
    """Python twin of the CASE chain's scope test: the first rule whose every declared
    condition holds. `path` is an ltree text; prefix match is ancestor-or-self."""
    tagset = set(tags)
    for r in rules:
        m = r.match
        if m.tags and not set(m.tags) <= tagset:
            continue
        if m.path_prefix is not None and not (path == m.path_prefix
                                              or path.startswith(m.path_prefix + ".")):
            continue
        if m.source is not None and source != m.source:
            continue
        return r
    return None


def age_factor(age_days: "float | None", rule: DecayRule) -> float:
    """1.0 at age 0 (and for undated content), asymptoting to the rule's floor."""
    if age_days is None:
        return 1.0
    return rule.floor + (1 - rule.floor) * math.exp(
        -_LN2 * max(0.0, age_days) / rule.half_life_days)
