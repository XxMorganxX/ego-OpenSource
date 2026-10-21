"""Process-wide, per-provider ceilings on concurrent inference calls.

Why a module-level registry rather than per-instance state: `InferenceRouter`'s docstring
claims one router per process, but the ingest process builds THREE — `ingest/daemon.py` for
`meaning_gate`, `ingest/daemon.py` again inside `_build_enrichers` (once per phase), and
`ingest/embedding.py` for the embedder. Per-instance semaphores would multiply the cap by the
number of routers, which is the whole thing this module exists to prevent.

The registry is shared process-wide, but each `ProviderLimiter` instance only GATES the
providers ITS OWN config gave a limit to: an instance constructed with `{provider: None}`
(or that never mentions `provider` at all) returns that provider's clients unwrapped, even
if some other instance in the process registered a cap for it. What IS shared across
instances is the semaphore itself — two limiters both configured with a cap for the same
provider draw on the one registered semaphore, and first-registration-wins still applies
when they disagree. In production this distinction never bites: every `InferenceRouter` in
a process loads the same `inference.toml`, so every `ProviderLimiter` is constructed with
the same limits and gates the same providers.

INVARIANT: a caller holds at most one permit at a time. Enrichment passes within a node run
serially, so this holds today. A NESTED inference call — a gated client invoked from inside
another gated client's call — would deadlock at cap 1 and must not be added without making
this re-entrant.

Local clients are never gated here: two daemons `isinstance`-check
`HTTPInferenceClient` and one calls `client.health()`, so a proxy would silently disable the
local health preflight. `[provider.local].concurrency` is rejected at config load.
"""

import threading

# `claude-p` is an alias for `claude-cli` (see _CLAUDE_CLI in inference_routing). Both must
# draw on one budget, or a config using the alias silently gets a second pool.
_ALIASES = {"claude-p": "claude-cli"}

_registry: "dict[str, threading.Semaphore]" = {}
_registry_limits: "dict[str, int]" = {}
_registry_lock = threading.Lock()


def budget_key(provider: str) -> str:
    """The registry key a provider draws its permits from."""
    return _ALIASES.get(provider, provider)


def reset_limits() -> None:
    """Test-only: drop every registered budget so a test starts from a clean registry."""
    with _registry_lock:
        _registry.clear()
        _registry_limits.clear()


class _GatedClient:
    """An InferenceClient that holds a provider permit for the duration of `complete`.

    Forwards `complete` ONLY — the single method on the InferenceClient protocol.
    Deliberately not `__getattr__`: a caller reaching for anything else (a future method, or
    an attribute like `ClaudeCliInferenceClient.last_usage`) raises AttributeError loudly
    instead of silently bypassing the gate. Forward it explicitly if that day comes.
    """

    def __init__(self, inner, sem: threading.Semaphore) -> None:
        self._inner = inner
        self._sem = sem
        # Forwarded explicitly (not via __getattr__): kb_core.ingest.inference.client_identity
        # reads these with getattr(..., None) defaults, so a missing attribute fails SILENTLY
        # (identity -> None) rather than raising. That's exactly the failure mode this proxy's
        # docstring says it refuses to allow. `last_usage` has no such getattr-default reader
        # on this path, so it stays unforwarded and still raises AttributeError.
        self.provider = getattr(inner, "provider", None)
        self.model = getattr(inner, "model", None)

    def complete(self, system_prompt: str, user_prompt: str, *,
                 schema: "dict | None" = None) -> str:
        self._sem.acquire()
        try:
            return self._inner.complete(system_prompt, user_prompt, schema=schema)
        finally:
            # finally, not else: a raising call must return its permit or the budget leaks
            # one slot per failure until the provider is fully blocked.
            self._sem.release()


class ProviderLimiter:
    """Registers the configured budgets, then gates clients against them.

    Any number of instances per process is fine — they all share the module registry, but
    each instance only gates providers ITS OWN `limits` gave a cap to (see the module
    docstring for why).
    """

    def __init__(self, limits: "dict[str, int | None]") -> None:
        # Which budget keys THIS instance actually has a cap for — wrap() consults this,
        # not just the shared registry, so an unbounded ({provider: None}) config never
        # gates a provider just because some other instance in the process registered one.
        self._gated: "set[str]" = set()
        for provider, limit in limits.items():
            if limit is None:
                continue
            key = budget_key(provider)
            self._gated.add(key)
            with _registry_lock:
                existing = _registry_limits.get(key)
                if existing is None:
                    _registry_limits[key] = limit
                    _registry[key] = threading.Semaphore(limit)
                elif existing != limit:
                    # First registration wins: rebinding would let a second config widen a
                    # cap the first one set, and threads already holding permits from the
                    # old semaphore would release into the new one.
                    print(f"WARNING: inference concurrency for {key!r} already set to "
                          f"{existing}; ignoring new limit {limit} (first registration "
                          "wins for the life of the process)", flush=True)

    def wrap(self, provider: str, client):
        """`client` gated if THIS instance's config capped its provider, otherwise `client`
        itself. Only the shared registry entry for a provider we ourselves gated is ever
        consulted — never another instance's."""
        key = budget_key(provider)
        if key not in self._gated:
            return client
        sem = _registry.get(key)
        if sem is None:
            return client
        return _GatedClient(client, sem)
