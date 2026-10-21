"""
FastAPI HTTP server exposing a local Gemma model (via Ollama + LangChain).

Endpoints
---------
GET    /health                      Ollama connectivity + model availability
POST   /chat                        One chat turn -> {"session", "reply"}
POST   /chat/stream                 Same, streamed as Server-Sent Events
GET    /sessions                    List active session ids        (debug opt-in, see below)
GET    /sessions/{id}               System prompt + full history   (debug opt-in, see below)
POST   /sessions/{id}/system        Set the system prompt for a session
DELETE /sessions/{id}               Reset history (or ?purge=true to drop it)

Run:
    python server.py
    # or:  uvicorn server:app --host 127.0.0.1 --port 8000

Network exposure (every request passes ExposureGuard, below):
    KB_INFERENCE_HOST           bind address for `python server.py` (falls back to HOST,
                                then 127.0.0.1). A non-loopback address is refused unless
                                KB_INFERENCE_ALLOW_REMOTE=1.
    KB_INFERENCE_ALLOW_REMOTE   1 = accept non-loopback binds and clients. Requires
                                KB_INFERENCE_TOKEN; the server refuses to start without it.
    KB_INFERENCE_ALLOWED_HOSTS  comma-separated extra Host-header names to accept, on top
                                of localhost / 127.0.0.1 / ::1 (DNS-rebinding defence).
    KB_INFERENCE_TOKEN          when set, every endpoint requires
                                `Authorization: Bearer <token>`. Unset = no token check.
    KB_INFERENCE_DEBUG_SESSIONS 1 = serve GET /sessions and GET /sessions/{id}, which return
                                prompts and full conversation history. Off = 404.

Upstream outages:
    When Ollama itself cannot be reached (stopped, restarting) or answers 503 (busy),
    /chat and /embed answer 503 with a `Retry-After` header (KB_INFERENCE_RETRY_AFTER
    seconds, default 30) instead of a 500. Callers treat that as "the provider is down,
    try later", not as a failure of the text they sent. Every other model error stays 500.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Optional, Union

import httpx
import ollama
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from conversation import ConversationManager
from embeddings import EmbeddingService

# Configure logging at import time so inference logs appear under either entrypoint
# (`python server.py` or `uvicorn server:app`). LOG_LEVEL=DEBUG also logs prompt/reply
# previews. basicConfig is a no-op if the root logger is already configured, so it
# won't fight a host that has set up its own handlers.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("kb_inference")


# --- network exposure ---------------------------------------------------------
# This server hands a GPU-resident model to whoever can reach it, and the in-memory
# sessions hold the prompts it was fed. It is meant to be reached from this machine only,
# so the defaults are: bind loopback, accept only loopback clients, accept only loopback
# Host headers (a page in a browser that DNS-rebinds its own name to 127.0.0.1 still sends
# its own name as Host, so it is refused), no token. Opening any of that is explicit.

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _truthy(value: "str | None") -> bool:
    return (value or "").strip().lower() in _TRUTHY


def is_loopback(host: str) -> bool:
    """True for localhost and any loopback IP literal (127.0.0.0/8, ::1), bracketed or not."""
    h = host.strip().strip("[]").lower().rstrip(".")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Exposure:
    token: "str | None"
    allowed_hosts: frozenset
    allow_remote: bool
    debug_sessions: bool


def load_exposure(env: Mapping[str, str]) -> Exposure:
    """Read the KB_INFERENCE_* exposure settings; raise on an unsafe combination."""
    token = (env.get("KB_INFERENCE_TOKEN") or "").strip() or None
    extra = {h.strip().strip("[]").lower().rstrip(".")
             for h in (env.get("KB_INFERENCE_ALLOWED_HOSTS") or "").split(",") if h.strip()}
    if "*" in extra:
        raise RuntimeError("KB_INFERENCE_ALLOWED_HOSTS does not accept '*': list the "
                           "host names clients will use")
    allow_remote = _truthy(env.get("KB_INFERENCE_ALLOW_REMOTE"))
    if allow_remote and not token:
        raise RuntimeError("KB_INFERENCE_ALLOW_REMOTE=1 requires KB_INFERENCE_TOKEN: "
                           "refusing to serve the model to the network without a token")
    return Exposure(token=token, allowed_hosts=_LOOPBACK_NAMES | extra,
                    allow_remote=allow_remote,
                    debug_sessions=_truthy(env.get("KB_INFERENCE_DEBUG_SESSIONS")))


def bind_host(env: Mapping[str, str], exposure: Exposure) -> str:
    """The address `python server.py` binds. Loopback unless remote access is opted into."""
    host = (env.get("KB_INFERENCE_HOST") or env.get("HOST") or "127.0.0.1").strip()
    if not is_loopback(host) and not exposure.allow_remote:
        raise SystemExit(
            f"refusing to bind KB Inference to non-loopback address {host!r}: set "
            "KB_INFERENCE_ALLOW_REMOTE=1 (and KB_INFERENCE_TOKEN) to expose it deliberately")
    return host


def _host_header_name(raw: str) -> str:
    """Host header -> bare lowercase name: '[::1]:8001' -> '::1', 'Localhost:80' -> 'localhost'."""
    raw = raw.strip().lower()
    if raw.startswith("["):
        return raw[1:raw.find("]")] if "]" in raw else ""
    if raw.count(":") == 1:
        raw = raw.rsplit(":", 1)[0]
    return raw.rstrip(".")


class ExposureGuard:
    """Pure-ASGI gate in front of every route, unknown paths included.

    Order: Host header (400), client address (403), bearer token (401). The settings are
    read per request through `get_exposure`, so the app's state is the one source of truth.
    """

    def __init__(self, app, get_exposure: Callable[[], Exposure]) -> None:
        self.app = app
        self.get_exposure = get_exposure

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        exp = self.get_exposure()
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        problem = None
        if _host_header_name(headers.get("host", "")) not in exp.allowed_hosts:
            problem = (400, "Invalid host header", {})
        elif not exp.allow_remote and scope.get("client") \
                and not is_loopback(str(scope["client"][0])):
            problem = (403, "Remote clients are not accepted", {})
        elif exp.token is not None:
            scheme, _, supplied = headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                    supplied.strip().encode(), exp.token.encode()):
                problem = (401, "Missing or invalid bearer token",
                           {"WWW-Authenticate": "Bearer"})
        if problem is None:
            await self.app(scope, receive, send)
            return
        status, detail, extra_headers = problem
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse({"detail": detail}, status_code=status,
                           headers=extra_headers)(scope, receive, send)


exposure = load_exposure(os.environ)

manager = ConversationManager()
embedder = EmbeddingService()
log.info(
    "KB Inference ready: model=%s base_url=%s temperature=%s seed=%s "
    "keep_alive=%s num_ctx=%s num_predict=%s think=%s embed_model=%s embed_dims=%s "
    "log_level=%s",
    manager.model, manager.base_url, manager.temperature, manager.seed,
    manager.keep_alive, manager.num_ctx, manager.num_predict,
    "model-default" if manager.reasoning is None else manager.reasoning, embedder.model,
    embedder.dimensions if embedder.dimensions is not None else "native", LOG_LEVEL,
)
log.info(
    "KB Inference exposure: token=%s allow_remote=%s allowed_hosts=%s debug_sessions=%s",
    "set" if exposure.token else "unset", exposure.allow_remote,
    ",".join(sorted(exposure.allowed_hosts)), exposure.debug_sessions,
)

app = FastAPI(
    title="KB Inference",
    description="Local Gemma (Ollama) chat server with LangChain conversation context.",
    version="0.1.0",
    # No interactive docs or schema: nothing calls them, and every extra route is
    # surface for a request that got past the Host/token guard.
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.state.exposure = exposure
app.add_middleware(ExposureGuard, get_exposure=lambda: app.state.exposure)

# Serialize access to the single local model. One GPU-resident model can't run two
# generations at once without each blowing past the client's timeout, so concurrent /chat
# and /chat/stream callers queue on this semaphore and run one at a time. Bounded by
# KB_INFERENCE_MAX_CONCURRENCY (default 1 = single-flight); raise it only if the hardware
# can genuinely run that many generations in parallel.
_MAX_CONCURRENCY = int(os.getenv("KB_INFERENCE_MAX_CONCURRENCY", "1"))
_gen_sem = asyncio.Semaphore(_MAX_CONCURRENCY)

# Embeddings get their own slot, separate from chat. They're light and bursty (you batch-embed
# a whole vault at once), so they must not queue behind a long Gemma generation on _gen_sem.
# Default 4; bounded by KB_EMBED_MAX_CONCURRENCY.
_EMBED_MAX_CONCURRENCY = int(os.getenv("KB_EMBED_MAX_CONCURRENCY", "4"))
_embed_sem = asyncio.Semaphore(_EMBED_MAX_CONCURRENCY)


# --- upstream (Ollama) outages ------------------------------------------------
# A stopped or restarting Ollama is an outage of the model backend, not a problem with the
# text a caller sent. Reported as 503 + Retry-After so KB_Core's client pauses the "local"
# provider and re-queues the note, instead of a 500 that marks the note failed for good.
# Only these shapes count: the ollama client raises a bare ConnectionError when it cannot
# connect at all, httpx's connect errors can surface directly, and Ollama answers 503 when
# its queue is full. A reset or read timeout happens AFTER the text was sent and may be
# about it (an over-long input can crash the runner), so those stay 500.
UPSTREAM_RETRY_AFTER_S = max(1, int(os.getenv("KB_INFERENCE_RETRY_AFTER", "30")))
_UPSTREAM_UNAVAILABLE_STATUS = frozenset({503})


def upstream_unavailable(exc: "BaseException | None") -> bool:
    """True when `exc` (or anything in its cause/context chain) says Ollama is unreachable
    or temporarily refusing work, rather than that this request failed."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        # Exact ConnectionError is the ollama client's "Failed to connect to Ollama";
        # its subclasses ConnectionResetError / BrokenPipeError / ConnectionAbortedError
        # are mid-request failures and do not count. ConnectionRefusedError does.
        if type(exc) is ConnectionError or isinstance(exc, ConnectionRefusedError):
            return True
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return True
        if isinstance(exc, ollama.ResponseError) \
                and exc.status_code in _UPSTREAM_UNAVAILABLE_STATUS:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _unavailable(what: str, exc: BaseException) -> HTTPException:
    """The 503 for an upstream outage, with a Retry-After the client can honour."""
    return HTTPException(
        status_code=503,
        detail=f"{what} unavailable: the model backend (Ollama) is unreachable or busy "
               f"({type(exc).__name__}: {exc}); retry after {UPSTREAM_RETRY_AFTER_S}s",
        headers={"Retry-After": str(UPSTREAM_RETRY_AFTER_S)},
    )


# --- request / response models ----------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., description="The user's message for this turn.")
    session: str = Field("default", description="Conversation id; history is kept per id.")
    system_prompt: Optional[str] = Field(
        None, description="If set, (re)assigns this session's system prompt before replying."
    )
    format: Optional[Union[str, dict]] = Field(
        None,
        description="Structured-output control: 'json' or a JSON Schema. When set, the "
                    "model is forced to emit a reply matching it.",
    )


class ChatResponse(BaseModel):
    session: str
    reply: str
    truncated: bool = False


class SystemPromptRequest(BaseModel):
    system_prompt: str


class EmbedRequest(BaseModel):
    input: Union[str, list[str]] = Field(
        ..., description="A single text or a list of texts to embed."
    )


class EmbedResponse(BaseModel):
    model: str
    dim: int
    count: int
    embeddings: list[list[float]]


# --- endpoints --------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    # Reuse the chat health probe's single /api/tags call to also report the embed model,
    # rather than hitting Ollama twice.
    h = manager.health()
    h["num_ctx"] = manager.num_ctx
    h["num_predict"] = manager.num_predict
    # null = the model's own default (gemma4 thinks), distinct from false = explicitly off.
    # The benchmark reads this to verify an arm's decoding settings actually took effect.
    h["think"] = manager.reasoning
    h["embedding"] = {
        "model": embedder.model,
        "model_available": embedder.model in h.get("models", []),
        "dimensions": embedder.dimensions,  # null = model's native dimensionality
    }
    return h


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if req.system_prompt is not None:
        manager.set_system_prompt(req.session, req.system_prompt)
    async with _gen_sem:                          # one generation at a time (see _gen_sem)
        try:
            reply = await manager.achat(req.session, req.message, format=req.format)
        except Exception as exc:
            if upstream_unavailable(exc):
                raise _unavailable("chat", exc) from exc
            raise
    return ChatResponse(session=req.session, reply=reply,
                        truncated=manager.was_truncated(req.session))


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    if req.system_prompt is not None:
        manager.set_system_prompt(req.session, req.system_prompt)

    async def event_stream():
        try:
            async with _gen_sem:                  # hold the slot for the whole stream
                async for token in manager.astream(req.session, req.message):
                    yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception as exc:  # noqa: BLE001 - surface errors to the SSE client
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    if not texts:
        raise HTTPException(status_code=422, detail="`input` must be a non-empty string or list.")
    if any(not isinstance(t, str) or not t.strip() for t in texts):
        raise HTTPException(status_code=422, detail="Each input must be a non-empty string.")
    async with _embed_sem:                        # own slot, separate from chat (see _embed_sem)
        try:
            vectors = await embedder.aembed(texts)
        except Exception as exc:
            if upstream_unavailable(exc):
                raise _unavailable("embed", exc) from exc
            raise
    dim = len(vectors[0]) if vectors else 0
    return EmbedResponse(model=embedder.model, dim=dim, count=len(vectors), embeddings=vectors)


def _require_debug_sessions() -> None:
    # Reading sessions back returns every prompt the server was fed (system prompt + full
    # history), i.e. whatever corpus text callers sent. Nothing in normal operation reads
    # them (KB_Core's client is stateless and purges), so they are a debug opt-in, and a
    # disabled endpoint is indistinguishable from a missing one.
    if not app.state.exposure.debug_sessions:
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/sessions")
def list_sessions() -> dict:
    _require_debug_sessions()
    return {"sessions": manager.list_sessions()}


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    _require_debug_sessions()
    if not manager.exists(session_id):
        raise HTTPException(status_code=404, detail=f"No session '{session_id}'")
    return {
        "session": session_id,
        "system_prompt": manager.get_system_prompt(session_id),
        "history": manager.get_history(session_id),
    }


@app.post("/sessions/{session_id}/system")
def set_system(session_id: str, req: SystemPromptRequest) -> dict:
    manager.set_system_prompt(session_id, req.system_prompt)
    return {"session": session_id, "system_prompt": req.system_prompt}


@app.delete("/sessions/{session_id}")
def reset_session(session_id: str, purge: bool = False) -> dict:
    if purge:
        return {"session": session_id, "deleted": manager.delete(session_id)}
    manager.reset(session_id)
    return {"session": session_id, "reset": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=bind_host(os.environ, exposure),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        proxy_headers=False,  # the loopback guard must see the socket peer, not X-Forwarded-For
    )
