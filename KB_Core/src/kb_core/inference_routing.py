"""Single source of truth for which model tier serves each inference task.

The registration table is TASKS (static, so ANY process can validate the whole config);
the routing itself lives in `inference.toml` (path: KB_INFERENCE_ROUTING env, default
./inference.toml — same resolution rule as enrichment.toml). The file must be COMPLETE:
every task here has a [task.<name>] block, every block names a task registered here, and
unknown keys are errors. Routing env vars (KB_INFERENCE_PROVIDER, KB_CONCEPT_LABEL_*,
KB_ANTHROPIC_MODEL, KB_CLAUDE_CLI_MODEL) are retired — set ones draw a startup warning.

Adding an inference task = one TASKS row + one [task.<name>] block + a row in
docs/llm-inference-map.md, all in the same change.
"""

import os
import tomllib
from dataclasses import dataclass

from kb_core.inference_limits import ProviderLimiter
from kb_core.ingest.inference import (EmbeddingClient, InferenceClient,
                                      build_inference_client)

COMPLETION = "completion"
EMBED = "embed"


@dataclass(frozen=True)
class TaskSpec:
    kind: str = COMPLETION
    # Embedding model changes invalidate every stored vector (nodes, chunks, entities,
    # concepts) — enforced here so a config edit can't silently orphan the corpus.
    must_be_local: bool = False


TASKS: "dict[str, TaskSpec]" = {
    "entity_gate":      TaskSpec(),
    "discover_classes": TaskSpec(),
    "classify_memory":  TaskSpec(),
    "extract_entities": TaskSpec(),
    "doc_kind":         TaskSpec(),
    "describe_entity":  TaskSpec(),
    "merge_judge":      TaskSpec(),
    "meaning_gate":     TaskSpec(),   # also the kb-denoise-instances backfill judge
    "concept_label":    TaskSpec(),
    "curate_parse":     TaskSpec(),
    "embed":            TaskSpec(kind=EMBED, must_be_local=True),
}

_PROVIDERS = ("local", "claude-cli", "claude-p", "anthropic", "agent")
_CLAUDE_CLI = ("claude-cli", "claude-p")
# Tiers build_inference_client forwards a per-task `timeout` to. `agent` takes one too:
# it is how long a caller waits for the tenant's machine before pausing the provider.
_TIMEOUT_PROVIDERS = _CLAUDE_CLI + ("agent",)
_TASK_KEYS = {"provider", "model", "timeout"}
_PROVIDER_KEYS = {"model", "concurrency", "timeout"}
_RETIRED_ENV = ("KB_INFERENCE_PROVIDER", "KB_CONCEPT_LABEL_PROVIDER",
                "KB_CONCEPT_LABEL_MODEL", "KB_ANTHROPIC_MODEL", "KB_CLAUDE_CLI_MODEL")


class RoutingError(RuntimeError):
    """inference.toml is missing, incomplete, or inconsistent with TASKS."""


@dataclass(frozen=True)
class TaskRoute:
    provider: str
    model: "str | None" = None
    timeout: "float | None" = None


def _read_toml(env: dict, path: "str | None") -> "tuple[dict, str]":
    """Load the routing file, returning (parsed, resolved_path)."""
    path = path or env.get("KB_INFERENCE_ROUTING", "inference.toml")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f), path
    except FileNotFoundError:
        raise RoutingError(
            f"inference routing file not found: {path} — every KB_Core process needs "
            "it (canonical copy: KB_Core/inference.toml; override path with "
            "KB_INFERENCE_ROUTING)") from None


def _parse_providers(data: dict, path: str) -> "tuple[dict[str, str | None], dict[str, int | None], dict[str, float]]":
    """Validate every [provider.*] block once -> (default models, concurrency limits,
    default timeouts).

    One implementation, two consumers (load_routing wants the models and timeouts,
    load_provider_limits wants the limits), so the validation cannot drift between them.
    """
    models: "dict[str, str | None]" = {}
    limits: "dict[str, int | None]" = {}
    timeouts: "dict[str, float]" = {}
    for name, block in (data.get("provider") or {}).items():
        if name not in _PROVIDERS:
            raise RoutingError(f"[provider.{name}] in {path}: unknown provider "
                               f"(one of {', '.join(_PROVIDERS)})")
        unknown = set(block) - _PROVIDER_KEYS
        if unknown:
            raise RoutingError(f"[provider.{name}] in {path}: unknown key(s) "
                               f"{sorted(unknown)} (allowed: {sorted(_PROVIDER_KEYS)})")
        if name == "local" and block.get("model"):
            raise RoutingError(
                f"[provider.local] in {path} takes no model — the local model is "
                "whatever KB_Inference serves (OLLAMA_MODEL, swapped via kb-model.sh)")
        limit = block.get("concurrency")
        if limit is not None:
            if name == "local":
                raise RoutingError(
                    f"[provider.local] in {path} takes no concurrency — local calls are "
                    "not gated client-side (two daemons isinstance-check "
                    "HTTPInferenceClient and one calls health(); a proxy would silently "
                    "disable the health preflight). Set KB_INFERENCE_MAX_CONCURRENCY on "
                    "the inference server instead")
            # bool first: True is an int in Python and `concurrency = true` must not pass.
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise RoutingError(
                    f"[provider.{name}] in {path}: concurrency must be an integer >= 1 "
                    f"(got {limit!r})")
        if "timeout" in block:
            if name != "agent":
                raise RoutingError(f"[provider.{name}] in {path}: timeout is only a "
                                   "[provider.agent] key (per-task timeouts go in [task.*])")
            t = block["timeout"]
            if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
                raise RoutingError(f"[provider.agent] in {path}: timeout must be a positive number")
            timeouts[name] = float(t)
        models[name] = block.get("model")
        limits[name] = limit
    return models, limits, timeouts


def load_provider_limits(env: "dict | None" = None,
                         path: "str | None" = None) -> "dict[str, int | None]":
    """Per-provider concurrency ceilings from inference.toml; None = unbounded.

    A separate entry point rather than a second return value on load_routing, whose
    `dict[str, TaskRoute]` return type is indexed directly by callers and by ~14 tests.
    """
    env = os.environ if env is None else env
    data, path = _read_toml(env, path)
    return _parse_providers(data, path)[1]


def load_routing(env: "dict | None" = None,
                 path: "str | None" = None) -> "dict[str, TaskRoute]":
    """Parse + validate inference.toml. Complete-by-construction: errors name the file
    and the fix, and fire for a missing task, an unknown task, an unknown key, a bad
    provider, a non-local embed, a model on [provider.local], or a timeout outside the
    claude-cli and agent tiers."""
    env = os.environ if env is None else env
    data, path = _read_toml(env, path)
    provider_defaults, _, provider_timeouts = _parse_providers(data, path)
    # KB_AGENT_JOB_TIMEOUT overrides the profile's [provider.agent] timeout (the
    # shipped tenant profile always sets one, so an override in build_inference_client's
    # `timeout is None` fallback never fired). Applied here, at the provider default, because
    # build_inference_client receives one merged timeout and cannot tell this default from
    # an explicit per-task one -- curate_parse's interactive 60 s stays the task's own.
    raw = (env.get("KB_AGENT_JOB_TIMEOUT") or "").strip()
    if raw:
        try:
            override = float(raw)
        except ValueError:
            override = 0.0
        if not override > 0:
            raise RoutingError(f"KB_AGENT_JOB_TIMEOUT must be a positive number of seconds "
                               f"(got {raw!r})")
        provider_timeouts["agent"] = override

    tasks = data.get("task") or {}
    missing = sorted(set(TASKS) - set(tasks))
    if missing:
        raise RoutingError(
            f"registered task(s) {missing} have no [task.<name>] entry in {path} — "
            "add one (provider = local | claude-cli | anthropic | agent)")
    unknown_tasks = sorted(set(tasks) - set(TASKS))
    if unknown_tasks:
        raise RoutingError(
            f"[task.*] entries {unknown_tasks} in {path} are not registered tasks — "
            "register in kb_core.inference_routing.TASKS or remove")

    routes: "dict[str, TaskRoute]" = {}
    for name, block in tasks.items():
        unknown = set(block) - _TASK_KEYS
        if unknown:
            raise RoutingError(f"[task.{name}] in {path}: unknown key(s) "
                               f"{sorted(unknown)} (allowed: {sorted(_TASK_KEYS)})")
        provider = block.get("provider")
        if provider not in _PROVIDERS:
            raise RoutingError(f"[task.{name}] in {path}: provider must be one of "
                               f"{', '.join(_PROVIDERS)} (got {provider!r})")
        if TASKS[name].must_be_local and provider != "local":
            raise RoutingError(
                f"[task.{name}] in {path} must stay provider = \"local\": changing "
                "the embedding model invalidates every stored vector (re-embed via "
                "kb-embed-backfill before trusting search again)")
        if "timeout" in block and provider not in _TIMEOUT_PROVIDERS:
            raise RoutingError(
                f"[task.{name}] in {path}: timeout is only forwarded to the "
                "claude-cli and agent tiers (HTTPInferenceClient reads "
                "KB_INFERENCE_TIMEOUT; anthropic takes none) — remove it")
        routes[name] = TaskRoute(
            provider=provider,
            model=block.get("model") or provider_defaults.get(provider),
            timeout=block.get("timeout", provider_timeouts.get(provider)))
    return routes


class InferenceRouter:
    """One per process. Resolves task -> client, lazily, sharing clients across tasks
    on the same (provider, model, timeout) -- except `agent`, keyed per task. Warns about retired routing env vars at
    construction so every process reports them with zero per-daemon wiring."""

    def __init__(self, env: "dict | None" = None, path: "str | None" = None, *,
                 pool=None) -> None:
        self._env = os.environ if env is None else env
        # Only `agent` clients use it (they enqueue llm_jobs rows); without one an `agent`
        # route fails loud at first use, in build_inference_client.
        self._pool = pool
        self.routes = load_routing(self._env, path)
        self.limits = load_provider_limits(self._env, path)
        # Registers the budgets process-wide. Safe to construct many routers: they share
        # one registry, which is the point (the ingest process builds three).
        self._limiter = ProviderLimiter(self.limits)
        self._cache: "dict[tuple, object]" = {}
        for var in _RETIRED_ENV:
            if self._env.get(var):
                print(f"WARNING: {var} is set but no longer read; routing lives in "
                      "inference.toml", flush=True)

    def _client(self, route: TaskRoute, task: str):
        # `agent` clients carry the task name into the job row, so they are keyed per task;
        # every other tier shares one client per (provider, model, timeout) as before.
        key = (route.provider, route.model, route.timeout,
               task if route.provider == "agent" else None)
        if key not in self._cache:
            client = build_inference_client(
                self._env, provider=route.provider, model=route.model,
                timeout=route.timeout, pool=self._pool, task=task)
            # Wrapped once, at cache-miss, so every consumer of this cache entry -- both
            # client_for() and embedder() -- inherits the cap with no call-site changes.
            # `local` has no budget by construction (config rejects the key), so
            # HTTPInferenceClient comes back bare and the daemons' isinstance/health()
            # preflight keeps working.
            self._cache[key] = self._limiter.wrap(route.provider, client)
        return self._cache[key]

    def client_for(self, task: str) -> InferenceClient:
        spec = TASKS.get(task)
        if spec is None:
            raise RoutingError(f"unknown inference task {task!r} — register it in "
                               "kb_core.inference_routing.TASKS")
        if spec.kind != COMPLETION:
            raise RoutingError(f"{task!r} is kind={spec.kind}, not a completion task — "
                               "use embedder()")
        return self._client(self.routes[task], task)

    def embedder(self) -> EmbeddingClient:
        # `embed` is validated local; HTTPInferenceClient implements both protocols,
        # so this shares an instance with any local completion task.
        return self._client(self.routes["embed"], "embed")

    def describe(self) -> str:
        """One startup-log line: 'task→provider[:model][ [≤cap]], …' sorted by task."""
        parts = []
        for task, route in sorted(self.routes.items()):
            cap = self.limits.get(route.provider)
            parts.append(f"{task}→{route.provider}"
                         + (f":{route.model}" if route.model else "")
                         + (f" [≤{cap}]" if cap else ""))
        return ", ".join(parts)
