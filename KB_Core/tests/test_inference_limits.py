"""inference_limits: the process-wide per-provider permit registry and its proxy."""
import threading
import time

import pytest

from kb_core.inference_limits import (
    ProviderLimiter, _registry, _registry_limits, budget_key,
)


class _Echo:
    def complete(self, system_prompt, user_prompt, *, schema=None):
        return "ok"


class _Boom:
    def complete(self, system_prompt, user_prompt, *, schema=None):
        raise RuntimeError("boom")


class _WithExtra:
    last_usage = {"cost": 1}

    def complete(self, system_prompt, user_prompt, *, schema=None):
        return "ok"


def test_unlimited_provider_returns_the_same_object():
    limiter = ProviderLimiter({"claude-cli": None})
    client = _Echo()
    assert limiter.wrap("claude-cli", client) is client


def test_unknown_provider_is_ungated():
    limiter = ProviderLimiter({"claude-cli": 2})
    client = _Echo()
    assert limiter.wrap("local", client) is client


def test_cap_is_never_exceeded():
    limiter = ProviderLimiter({"claude-cli": 2})
    lock = threading.Lock()
    live = peak = 0
    # Deterministic overlap: with a cap of 2, exactly two threads can hold permits at
    # once. Both must reach this barrier before either proceeds, which proves real
    # overlap happened instead of hoping a sleep was long enough on a loaded machine.
    # If the cap were wrongly 1, only one thread would ever reach the barrier and it
    # would never fill, so the wait times out and the test fails loudly (rather than
    # passing vacuously). The peak counter still catches the cap being wrongly 8.
    barrier = threading.Barrier(2)

    class _Slow:
        def complete(self, system_prompt, user_prompt, *, schema=None):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            barrier.wait(timeout=5)
            time.sleep(0.05)
            with lock:
                live -= 1
            return "ok"

    gated = limiter.wrap("claude-cli", _Slow())
    threads = [threading.Thread(target=gated.complete, args=("s", "u")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2, f"cap of 2 exceeded: peak was {peak}"


def test_permit_is_released_when_the_call_raises():
    limiter = ProviderLimiter({"claude-cli": 1})
    gated = limiter.wrap("claude-cli", _Boom())
    for _ in range(3):
        with pytest.raises(RuntimeError, match="boom"):
            gated.complete("s", "u")
    # Three failures must have returned three permits, so this acquire cannot block.
    assert _registry["claude-cli"].acquire(timeout=0.1)


def test_claude_p_alias_shares_one_budget():
    """Two limiters, each constructed WITH the budget it uses (not relying on some
    other instance's registration): claude-p and claude-cli still collapse onto the
    same semaphore, proving both alias collapsing and cross-instance sharing."""
    assert budget_key("claude-p") == "claude-cli"
    a = ProviderLimiter({"claude-cli": 3}).wrap("claude-p", _Echo())
    b = ProviderLimiter({"claude-p": 3}).wrap("claude-cli", _Echo())
    assert a._sem is b._sem


def test_unbounded_instance_ignores_another_instances_registered_cap():
    """Regression for the registry over-reach defect: wrap() must consult the calling
    instance's OWN configuration, not just the shared registry. A limiter constructed
    with {"claude-cli": None} (unbounded) must return the client UNWRAPPED even after
    some other limiter in the process has registered a cap for claude-cli."""
    ProviderLimiter({"claude-cli": 4})   # some other instance registers a cap
    client = _Echo()
    unbounded = ProviderLimiter({"claude-cli": None}).wrap("claude-cli", client)
    assert unbounded is client


def test_two_limiters_in_one_process_share_one_budget():
    """The ingest daemon's real shape: three routers, one budget."""
    first = ProviderLimiter({"claude-cli": 2})
    second = ProviderLimiter({"claude-cli": 2})
    third = ProviderLimiter({"claude-cli": 2})
    sems = {id(lim.wrap("claude-cli", _Echo())._sem) for lim in (first, second, third)}
    assert len(sems) == 1


def test_conflicting_limit_warns_and_keeps_the_first(capsys):
    ProviderLimiter({"claude-cli": 2})
    first_sem = _registry["claude-cli"]
    ProviderLimiter({"claude-cli": 8})
    out = capsys.readouterr().out
    assert "already set to 2" in out
    assert "ignoring new limit 8" in out
    assert _registry_limits["claude-cli"] == 2
    # The hazard the code comment names: a second registration must not rebind the
    # Semaphore object either, or threads already holding permits from the first one
    # would release into a different object than the one new acquirers block on.
    assert _registry["claude-cli"] is first_sem


def test_proxy_forwards_only_complete():
    gated = ProviderLimiter({"claude-cli": 1}).wrap("claude-cli", _WithExtra())
    assert gated.complete("s", "u") == "ok"
    with pytest.raises(AttributeError):
        gated.last_usage


def test_schema_kwarg_is_forwarded():
    seen = {}

    class _Capture:
        def complete(self, system_prompt, user_prompt, *, schema=None):
            seen["schema"] = schema
            return "ok"

    gated = ProviderLimiter({"claude-cli": 1}).wrap("claude-cli", _Capture())
    gated.complete("s", "u", schema={"type": "object"})
    assert seen["schema"] == {"type": "object"}
