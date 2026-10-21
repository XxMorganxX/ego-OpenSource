"""Client for the local Gemma inference engine (the sibling KB_Inference server).

KB_Inference is a FastAPI server wrapping Gemma via Ollama. We use it statelessly: one
POST /chat per LLM pass with a fresh session id (so no prior history bleeds in) and a
best-effort purge afterwards. Transport only — stdlib urllib, no extra dependency.
"""

import email.utils
import errno
import json
import os
import socket
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

# Where KB_Inference actually listens: scripts/kb-svc.sh runs it on KB_INFERENCE_PORT
# (default 8001) and the dashboard API on KB_DASHBOARD_PORT (default 8000). This
# default used to be 8000, so any process started without KB_INFERENCE_URL POSTed at
# the dashboard and got a 404 — silently, because both /embed callers are fail-open.
# A consolidation run lost all of its concept embeddings that way. The daemons never hit
# it (kb-svc.sh exports the URL); hand-run CLIs always did.
LOCAL_INFERENCE_URL = "http://127.0.0.1:8001"


def inference_auth_headers(env=None) -> dict:
    """{"Authorization": "Bearer …"} when KB_INFERENCE_TOKEN is set, else {}.

    KB_Inference requires the token on every route once it is configured with one (the
    same variable, read from KB_Core/.env by kb-svc.sh for the server and every caller).
    Unset = no header, matching a server that has no token.
    """
    token = ((env if env is not None else os.environ).get("KB_INFERENCE_TOKEN") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


@runtime_checkable
class InferenceClient(Protocol):
    def complete(self, system_prompt: str, user_prompt: str, *,
                 schema: dict | None = None) -> str:
        """One stateless LLM turn: system + user prompt -> the model's reply text.

        `schema` (optional) is a JSON Schema the reply is enforced to match (structured
        output) — forwarded to the engine as its `format`.
        """
        ...


@runtime_checkable
class EmbeddingClient(Protocol):
    def embed(self, texts: "list[str]") -> "list[list[float]]":
        """Batch-embed texts -> one vector per input, in order."""
        ...


class InferenceError(RuntimeError):
    """The inference engine was unreachable or returned an error (transport-level)."""


class ProviderUnavailable(InferenceError):
    """The provider is temporarily refusing every call (quota window, auth outage) —
    the work is deferrable, not failed. `until` is the provider's stated recovery
    time when it gave one (tz-aware), else None, which downstream treats as "re-probe
    on the default interval". Raised instead of the plain InferenceError so the
    enrichment worker can defer the node rather than terminally fail it."""

    def __init__(self, message: str, *, provider: str,
                 until: "datetime | None" = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.until = until


def build_inference_client(env, *, provider: "str | None" = None,
                           model: "str | None" = None,
                           output_format: "str | None" = None,
                           timeout: "float | None" = None,
                           hardened: bool = True,
                           pool=None,
                           task: "str | None" = None) -> "InferenceClient":
    """Pick a completion backend, with explicit overrides for mixed-model routing.

    `provider="anthropic"` → Claude Messages API; `claude-cli` → `claude -p`; `agent` → the
    tenant's job queue; anything else (default) → the local KB_Inference HTTP server at
    `KB_INFERENCE_URL`. In production `provider`/`model` always come from inference.toml via
    InferenceRouter; the env fallbacks below (KB_INFERENCE_PROVIDER, KB_ANTHROPIC_MODEL,
    KB_CLAUDE_CLI_MODEL) are retired as routing settings and serve only direct callers such
    as tests.
    Embeddings are unaffected — the embedding enricher always uses the local server.

    `provider` and `model` take precedence over the environment. They let one process route a
    specific pass to a different backend without mutating process-global configuration.

    `hardened` defaults to `True`, as `ClaudeCliInferenceClient`'s own default does: this
    factory is what enrichment actually gets, and enrichment is the path that feeds untrusted
    corpus text to the model. Only relevant to the `claude-cli`/`claude-p` branch; `local` and
    `anthropic` don't spawn a tool-bearing subprocess, so the flag is meaningless for them.

    `output_format` and `timeout` are forwarded to the **claude-cli branch only**.
    `output_format="json"` makes that client parse the CLI's result envelope so a caller can
    read `last_usage` (tokens + cost) back off it — the read path's disclosure surface needs
    per-query provider cost and text mode cannot supply it. `timeout` is how a caller's own per-call
    limit (e.g. LLM_CALL_TIMEOUT_S = 45) overrides the inherited KB_INFERENCE_TIMEOUT = 300 for one
    caller without mutating process-global configuration.

    Neither is offered to the `local` or `anthropic` branches, and that is a decision rather
    than an omission. `HTTPInferenceClient` already reads `KB_INFERENCE_TIMEOUT` itself
    (below), so forwarding `timeout` would give one setting two sources of truth that
    disagree whenever a caller passes one; and neither of those clients spawns a subprocess
    or has an envelope to parse, so `output_format` has nothing to name there — accepting it
    would imply a usage/cost guarantee those paths do not make.

    `agent` (tenant daemons) returns an AgentQueueClient: the completion becomes an llm_jobs
    row answered by the tenant's own kb-agent. It needs `pool` (raises InferenceError
    without one) and takes `task` (recorded on the job row), `model` (a hint for the agent)
    and `timeout` (how long a caller waits before provider `agent` pauses; else
    KB_AGENT_JOB_TIMEOUT, default 600 s — load_routing already applies KB_AGENT_JOB_TIMEOUT
    over the profile's [provider.agent] timeout). KB_AGENT_PAUSE_MINUTES (default 15) sets the
    pause that follows a timeout.
    """
    provider = (provider or env.get("KB_INFERENCE_PROVIDER") or "local").strip().lower()
    # `… or DEFAULT`, not `.get(key, DEFAULT)`: a launcher that forwards `${VAR:-}` sets the var
    # to '' when the user didn't override it, and .get returns that '' (key present) — which would
    # pass an empty model downstream (claude -p --model '' -> 400). Empty falls back to the default.
    if provider == "agent":
        if pool is None:
            raise InferenceError("provider 'agent' needs a connection pool: construct the "
                                 "router as InferenceRouter(env, pool=pool)")
        from kb_core.ingest.agent_queue_client import AgentQueueClient   # psycopg: lazy
        return AgentQueueClient(
            pool, task=task or "unknown",
            timeout_s=float(timeout if timeout is not None
                            else env.get("KB_AGENT_JOB_TIMEOUT") or 600),
            pause_minutes=float(env.get("KB_AGENT_PAUSE_MINUTES") or 15),
            model_hint=model)
    if provider == "anthropic":
        from kb_core.ingest.anthropic_client import DEFAULT_MODEL, AnthropicInferenceClient
        return AnthropicInferenceClient(
            model=model or env.get("KB_ANTHROPIC_MODEL") or DEFAULT_MODEL)
    if provider in ("claude-cli", "claude-p"):
        from kb_core.ingest.claude_cli_client import (DEFAULT_CLI_MODEL,
                                                      DEFAULT_CLI_TIMEOUT,
                                                      ClaudeCliInferenceClient)
        return ClaudeCliInferenceClient(
            model=model or env.get("KB_CLAUDE_CLI_MODEL") or DEFAULT_CLI_MODEL,
            binary=env.get("KB_CLAUDE_CLI_BIN", "claude"),
            # `or DEFAULT`, not `.get(key, DEFAULT)` -- the same ${VAR:-} trap the model
            # line above documents. env.get returns '' when a launcher forwarded an unset
            # var, and float('') raised ValueError at construction.
            timeout=float(timeout if timeout is not None
                          else env.get("KB_INFERENCE_TIMEOUT") or DEFAULT_CLI_TIMEOUT),
            output_format=output_format,
            hardened=hardened)
    return HTTPInferenceClient(env.get("KB_INFERENCE_URL", LOCAL_INFERENCE_URL),
                               token=env.get("KB_INFERENCE_TOKEN"))


def client_identity(client) -> "str | None":
    """"provider:model" for a client that declares an identity; None otherwise.

    Enrichment provenance (the ingest log's providers detail). Never raises — a client
    without a `provider` attribute (e.g. a test fake) simply contributes nothing.
    """
    provider = getattr(client, "provider", None)
    if not provider:
        return None
    return f"{provider}:{getattr(client, 'model', None) or '?'}"


# How long an unreachable KB_Inference pauses the provider "local". Short on purpose: the
# usual cause is the server still starting (every daemon boots at once) or restarting.
LOCAL_UNREACHABLE_PAUSE = timedelta(seconds=60)

# Errors that mean the request never reached a server: nothing listening, no route, no
# such host. Only these become ProviderUnavailable — a timeout or a reset happen AFTER the
# document was sent, so they may be about the document and stay a plain InferenceError.
_NEVER_REACHED = frozenset({errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH,
                            errno.EHOSTDOWN})


def _never_reached(exc: BaseException) -> bool:
    reason = (exc.reason if isinstance(exc, urllib.error.URLError)
              and not isinstance(exc, urllib.error.HTTPError) else exc)
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return True
    return isinstance(reason, OSError) and reason.errno in _NEVER_REACHED


# Bounds on a server-stated Retry-After. The floor stops a 0/1 s hint from re-claiming a
# batch into a dead backend in a tight loop; the ceiling stops a bogus huge value from
# parking the lane for hours when the usual cause (Ollama restarting) takes seconds.
RETRY_AFTER_MIN = timedelta(seconds=5)
RETRY_AFTER_MAX = timedelta(minutes=15)


def _retry_after(headers, now: datetime) -> timedelta:
    """The pause a 503's Retry-After asks for (delta-seconds or an HTTP-date), clamped to
    [RETRY_AFTER_MIN, RETRY_AFTER_MAX]; LOCAL_UNREACHABLE_PAUSE when absent or unparseable."""
    raw = (headers.get("Retry-After") if headers is not None else None) or ""
    raw = raw.strip()
    delay: "timedelta | None" = None
    if raw.isdigit():
        delay = timedelta(seconds=int(raw))
    elif raw:
        try:
            when = email.utils.parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delay = when - now
        except (TypeError, ValueError, IndexError, OverflowError):
            delay = None
    if delay is None:
        return LOCAL_UNREACHABLE_PAUSE
    return min(max(delay, RETRY_AFTER_MIN), RETRY_AFTER_MAX)


def _transport_error(what: str, base: str, exc: Exception) -> InferenceError:
    """The error for a failed KB_Inference call. An unreachable server is an outage of
    the provider "local", not a failure of the document: ProviderUnavailable lets the
    enrichment worker defer the node and pause the lane (a first boot used to mark every
    note that won the race with inference 'failed' for good).

    The same holds one hop further: a running KB_Inference whose model backend (Ollama)
    is down answers 503 + Retry-After, which becomes ProviderUnavailable paused for that
    long (clamped). Every other HTTP status stays a plain InferenceError."""
    now = datetime.now(timezone.utc)
    if _never_reached(exc):
        return ProviderUnavailable(
            f"KB_Inference unreachable at {base}: {exc}", provider="local",
            until=now + LOCAL_UNREACHABLE_PAUSE)
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 503:
        return ProviderUnavailable(
            f"KB_Inference at {base} reports its model backend unavailable: {exc}",
            provider="local", until=now + _retry_after(exc.headers, now))
    return InferenceError(f"{what} failed: {exc}")


class HTTPInferenceClient:
    """Talks to KB_Inference over HTTP. Each call is an independent, history-free turn.

    A server that cannot be reached at all, or that answers 503 (its Ollama backend is
    down), raises ProviderUnavailable(provider="local"); every other transport or protocol
    failure raises InferenceError."""

    provider = "local"

    def __init__(self, base_url: str = LOCAL_INFERENCE_URL, *,
                 timeout: float | None = None, token: "str | None" = None) -> None:
        self._base = base_url.rstrip("/")
        # Bearer token for a KB_Inference started with KB_INFERENCE_TOKEN; None reads the
        # process environment, so every hand-run caller picks it up without plumbing.
        self._auth = inference_auth_headers(
            {"KB_INFERENCE_TOKEN": token} if token is not None else None)
        # Default from the environment so a deployment can lift it above the stock 120s for a
        # slow, single-flight model — a queued generation has to outlast the one ahead of it.
        self._timeout = timeout if timeout is not None else float(
            os.environ.get("KB_INFERENCE_TIMEOUT", "300"))
        self._model: "str | None" = None       # chat model, resolved lazily from /health
        self.embed_model: "str | None" = None  # stashed from the last /embed response

    @property
    def model(self) -> str:
        """The server-side chat model name, for provenance. Resolved from /health on first
        read and cached; a failed probe reads "?" WITHOUT caching (the next read retries —
        probes run right after a successful chat call, so failures are rare) and the short
        timeout bounds the stall either way."""
        if self._model is None:
            try:
                req = urllib.request.Request(f"{self._base}/health", headers=self._auth)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    served = json.loads(resp.read()).get("model")
                if served:
                    self._model = str(served)
            except Exception:
                pass
        return self._model or "?"

    def complete(self, system_prompt: str, user_prompt: str, *,
                 schema: dict | None = None) -> str:
        session = f"kb-enrich-{uuid.uuid4()}"
        body: dict = {
            "session": session,
            "system_prompt": system_prompt,
            "message": user_prompt,
        }
        if schema is not None:
            body["format"] = schema          # enforce structured (JSON-schema) output
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self._base}/chat", data=payload, method="POST",
            headers={"Content-Type": "application/json", **self._auth},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                reply = json.loads(resp.read())["reply"]
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as exc:
            raise _transport_error("inference call", self._base, exc) from exc
        finally:
            self._purge(session)
        return reply

    def _purge(self, session: str) -> None:
        # Drop the ephemeral session so the server doesn't accumulate history. Best-effort.
        try:
            req = urllib.request.Request(
                f"{self._base}/sessions/{session}?purge=true", method="DELETE",
                headers=self._auth)
            urllib.request.urlopen(req, timeout=self._timeout).close()
        except Exception:
            pass

    def embed(self, texts: "list[str]") -> "list[list[float]]":
        payload = json.dumps({"input": list(texts)}).encode()
        req = urllib.request.Request(
            f"{self._base}/embed", data=payload, method="POST",
            headers={"Content-Type": "application/json", **self._auth})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read())
                self.embed_model = data.get("model") or self.embed_model
                return data["embeddings"]
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as exc:
            raise _transport_error("embed call", self._base, exc) from exc

    def health(self) -> dict:
        req = urllib.request.Request(f"{self._base}/health", headers=self._auth)
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read())
