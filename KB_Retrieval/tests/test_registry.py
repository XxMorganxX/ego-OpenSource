"""Name -> strategy resolution. Mirrors the house registry pattern
(adapters/base.py DEFAULT_INGESTOR, ingest/inference.py provider selection)."""

import pytest

from kb_retrieval.contract import HYBRID_CAPS, Cost, RetrievalResult, Trace
from kb_retrieval.registry import (DEFAULT_STRATEGY, STRATEGIES, UnknownStrategy,
                                   get_strategy, register)


class _Fake:
    name = "fake"

    def __init__(self, kb, **kw):
        self.kb = kb
        self.kw = kw

    def capabilities(self):
        return HYBRID_CAPS

    def search(self, q):
        return RetrievalResult(rows=[], confidence={"level": "none", "n": 0},
                               applied_filters={}, ignored_filters=[],
                               cost=Cost(), trace=Trace())


def test_default_strategy_is_hybrid():
    assert DEFAULT_STRATEGY == "hybrid"


def test_register_then_resolve(monkeypatch):
    monkeypatch.setitem(STRATEGIES, "fake", _Fake)
    s = get_strategy("fake", kb=object())
    assert isinstance(s, _Fake)


def test_unknown_name_names_what_is_available():
    with pytest.raises(UnknownStrategy) as exc:
        get_strategy("nope", kb=object())
    assert "nope" in str(exc.value)
    assert "hybrid" in str(exc.value)


def test_kwargs_reach_the_strategy(monkeypatch):
    monkeypatch.setitem(STRATEGIES, "fake", _Fake)
    s = get_strategy("fake", kb=object(), judge="none")
    assert s.kw == {"judge": "none"}


def test_register_rejects_a_non_conforming_class():
    class Bad:
        name = "bad"

    with pytest.raises(TypeError):
        register("bad", Bad)


def test_second_builtin_resolves_after_hybrid_already_populated_strategies(monkeypatch):
    """Regression for the lazy-load guard: the old guard was
    `if not STRATEGIES: _load_builtins()`. hybrid.py registers at import time, so
    once anything has imported kb_retrieval.hybrid, STRATEGIES is non-empty and
    the loader never runs again -- a second builtin module (e.g. crawl.py) would
    never get imported, and get_strategy("cluster-crawl") would raise
    UnknownStrategy even with the module present on disk.

    Reproduce the exact precondition: STRATEGIES already carries "hybrid" (real
    usage guarantees this by the time anything calls get_strategy a second time),
    but the loader itself has not run yet.
    """
    import kb_retrieval.hybrid  # noqa: F401 -- populate STRATEGIES like real use does
    assert "hybrid" in STRATEGIES

    monkeypatch.setattr("kb_retrieval.registry._loaded", False)
    monkeypatch.delitem(STRATEGIES, "cluster-crawl", raising=False)

    def _fake_load_builtins():
        STRATEGIES.setdefault("cluster-crawl", _Fake)

    monkeypatch.setattr("kb_retrieval.registry._load_builtins", _fake_load_builtins)

    s = get_strategy("cluster-crawl", kb=object())
    assert isinstance(s, _Fake)
