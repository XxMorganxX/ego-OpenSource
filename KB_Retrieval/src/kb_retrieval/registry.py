"""Name -> strategy. The swap point.

Mirrors the house pattern: a dict, a DEFAULT_*, and a resolver that fails with a
message naming what IS available (adapters/base.py, ingest/inference.py's build_inference_client).
"""

from kb_retrieval.contract import RetrievalStrategy  # noqa: F401  (re-exported type)

STRATEGIES: "dict[str, type]" = {}

# Tracks whether _load_builtins() has run -- NOT whether STRATEGIES is non-empty.
# hybrid.py registers itself at import time, so anything that imports it directly
# (a test, another strategy module) makes STRATEGIES truthy before get_strategy
# ever runs. Using that truthiness as the "have we loaded" signal (the old guard:
# `if not STRATEGIES`) meant the loader silently never ran again once that
# happened -- so a second builtin (e.g. crawl.py) would never get imported and
# get_strategy("cluster-crawl") raised UnknownStrategy even with the module
# present on disk. This flag tracks the loader's own state instead.
_loaded = False

# What a caller gets when it does not choose: the cheap strategy, so nothing
# silently opts a caller into one whose cost_class is "expensive".
#
# Mode A baseline: the first must_recall@20 figure was measured before two
# `ORDER BY ..., id` tiebreaks were added to KnowledgeBase.search() (its FTS-only
# branch and `fts` CTE) to close a real
# non-determinism, which changes ordering on score ties. That number was therefore
# unrecoverable by comparison -- the equivalence proof compares new-vs-legacy and
# both carry the fix -- so the baseline was re-measured rather than reasoned about,
# with the legacy plans through search_memories rather than the benchmark's
# substituted plans.
#
# evals/retrieval/validate.py must be green before a measurement: stale gold
# references make run.py refuse to score -- and it was green at the start of every
# benchmark cell, since bench.py hard-gates on it (bench.main() runs validate.py
# first, exit 2). It is NOT a standing guarantee: live entity/concept drift re-reds
# it. Re-run it before the next measurement rather than assuming the last run's
# verdict.
DEFAULT_STRATEGY = "hybrid"


class UnknownStrategy(ValueError):
    """Asked for a strategy name that is not registered."""


def register(name: str, cls: type) -> None:
    """Register a strategy class. Rejects anything that does not satisfy the
    Protocol at registration time rather than at first search().

    NOTE: this checks members by hand rather than with `issubclass`. A
    `runtime_checkable` Protocol carrying a non-method member (`name: str`) does
    NOT support issubclass -- Python raises
    "Protocols with non-method members don't support issubclass()". `isinstance`
    works, but we hold classes here, not instances.
    """
    missing = [m for m in ("name", "capabilities", "search") if not hasattr(cls, m)]
    if missing:
        raise TypeError(
            f"{cls.__name__} does not satisfy RetrievalStrategy; missing: "
            f"{', '.join(missing)}")
    STRATEGIES[name] = cls


def get_strategy(name: "str | None", kb, **kw) -> RetrievalStrategy:
    global _loaded
    if not _loaded:
        _load_builtins()
        _loaded = True
    key = name or DEFAULT_STRATEGY
    cls = STRATEGIES.get(key)
    if cls is None:
        known = ", ".join(sorted(STRATEGIES)) or DEFAULT_STRATEGY
        raise UnknownStrategy(f"unknown strategy {key!r}; registered: {known}")
    return cls(kb, **kw)


def _load_builtins() -> None:
    """Import strategy modules for their register() side effect. Called lazily to
    avoid a circular import (both strategies import register from here)."""
    from kb_retrieval import hybrid  # noqa: F401
    from kb_retrieval.crawl import strategy  # noqa: F401
