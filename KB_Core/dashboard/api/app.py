"""Thin FastAPI surface over KnowledgeBase. Parse args -> call core -> format JSON."""

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import anyio
import psycopg
from fastapi import (Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket,
                     WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from kb_core import db_browse, library_client, skills
from kb_core.cf_access import AuthRejected, CfAccessConfig, CfAccessVerifier, token_from_headers
from kb_core.claude_auth import NOT_IN_USE, claude_auth_status, claude_cli_in_use
from kb_core.concept_curation import (CURATION_SCHEMA, build_curation_prompt,
                                      describe_op, parse_curation)
from kb_core.curation import CurationConflict, CurationService
from kb_core.domain import Node, NodePatch, NodeRecord
from kb_core.inference_routing import InferenceRouter
from dashboard.api import evals_reader
from dashboard.api.schemas import (
    AnalysisExcludeIn, ApproveMergeIn, BlockClassIn, CreateIn, DismissStuckIn,
    MergeClassesIn, MoveIn, ReparentInstanceIn, RejectMergeIn, RetireIn, RetryFailedIn,
    SetAgeDecayIn, SetClassDescriptionIn, SetToolEnabledIn, SignificanceIn, UpdateIn,
)
from kb_core.entities import (
    ENTITY_CLASS, ENTITY_ROOT, ENTITY_SOURCE, ENTITY_TYPE, STATUS_BLOCKED,
    normalize_entity_name,
)
from kb_core.knowledge_base import ConsolidationBusy, KnowledgeBase
from kb_core.paths import sanitize_slug
from kb_core.tenancy import (TenantContext, TenantKBProxy, TenantPools, current_principal,
                             current_tenant, default_tenant_slug, require_tenant)
from kb_core.tenants import OWNER_SLUG, Registry, Tenant, load_registry
from kb_core.validation import ValidationError

log = logging.getLogger("kb-dashboard")

_TREE_LIMIT = 100_000  # personal KB: load every node's metadata in one shot
_CHANGE_CHANNEL = "kb_change"
# ChangeHub.stop(): re-cancel the listener up to this many times, this far apart (5 s).
_STOP_ATTEMPTS = 50
_STOP_POLL_S = 0.1

# Host allow-list for the API. The dashboard's legitimate traffic reaches this backend as
# 127.0.0.1:8000 (the vite preview proxy sets changeOrigin), so any request arriving with a
# public Host — e.g. a tunnel ingress pointed straight at this API, bypassing the access gate
# on your public front-door — is rejected here with 400. Defence-in-depth behind the edge;
# override for other front-doors via KB_API_ALLOWED_HOSTS.
_DEFAULT_ALLOWED_HOSTS = (
    "localhost,127.0.0.1,localhost:8000,127.0.0.1:8000,"
    "localhost:5173,127.0.0.1:5173,localhost:8091,127.0.0.1:8091,testserver"
)

# Origins allowed to issue MUTATING requests. Same list CORSMiddleware uses for reads —
# kept as a module constant so the two can never drift apart. Localhost dev origins are
# always present; a public front-door (e.g. a vite preview behind an access-gated tunnel)
# is added via KB_DASHBOARD_ORIGINS (comma-separated). Without your front-door listed,
# every remote write — retry-failed, curation, merge approval — 403's while reads still
# work. Listing it keeps CSRF intact: a browser won't forge Origin for an attacker page.
_ALLOWED_ORIGINS = tuple(
    ["http://localhost:5173", "http://127.0.0.1:5173"]
    + [o.strip() for o in os.environ.get("KB_DASHBOARD_ORIGINS", "").split(",") if o.strip()]
)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_LOOPBACK_ORIGIN = re.compile(r"^http://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$")


def _ws_origin_allowed(origin: "str | None") -> bool:
    """Whether a WebSocket handshake may open the change feed.

    The CSRF guard is HTTP middleware and never sees a websocket scope, and browsers let
    any page open a cross-origin WebSocket: without this check, every site the owner
    visited could subscribe to /api/ws and watch {table, op, id} for every change. No
    Origin = a non-browser client (allowed, like the HTTP guard). Loopback origins are
    allowed because `vite preview` proxies the socket WITHOUT rewriting Origin, so the
    dashboard's own feed arrives as http://127.0.0.1:<KB_FRONTEND_PORT>; a remote web
    page can never carry a loopback origin. Anything else must be listed (the
    KB_DASHBOARD_ORIGINS front door)."""
    if origin is None:
        return True
    return origin in _ALLOWED_ORIGINS or bool(_LOOPBACK_ORIGIN.match(origin))

_RUN_TIMEOUT_S = float(os.environ.get("KB_CONSOLIDATE_RUN_TIMEOUT", "1800"))

# single-user dashboard: the acting identity is whoever deployed it; override via
# KB_DASHBOARD_IDENTITY (scripted and LLM judges write through KnowledgeBase with
# their own identity, so this only stamps the human-in-the-browser actions below).
_DASHBOARD_IDENTITY = os.environ.get("KB_DASHBOARD_IDENTITY", "owner")


class _AuthFailure(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


class DashboardAuthRequired(RuntimeError):
    """The registry holds an enabled non-owner tenant but no verifier is configured: with
    auth off every request would be served as the OWNER, tenants included.
    Fail closed instead."""


def _has_enabled_tenants(registry: "Registry") -> bool:
    return bool(registry.tenants(include_owner=False))


class _TenantMiddleware:
    """Pure ASGI: binds the tenant for http AND websocket scopes in the SAME task the
    endpoint runs in (BaseHTTPMiddleware would not cover websocket upgrades). Fails
    closed: a rejected identity is its 401/403/503, and anything else the resolver
    raises is a 500 — never a request served without a bound tenant."""

    def __init__(self, app, resolve) -> None:
        self._app, self._resolve = app, resolve

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or scope.get("path") == "/healthz":
            return await self._app(scope, receive, send)
        headers: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            name, value = k.decode("latin-1").lower(), v.decode("latin-1")
            # HTTP/2 may split cookies over several headers: join them, never let one win.
            if name == "cookie" and name in headers:
                headers[name] = f"{headers[name]}; {value}"
            else:
                headers[name] = value
        try:
            ctx, email = await anyio.to_thread.run_sync(self._resolve, headers)
        except _AuthFailure as exc:
            return await self._reject(scope, send, exc.status, exc.detail, ws_code=1008)
        except Exception:
            log.exception("dashboard auth check failed")
            return await self._reject(scope, send, 500, "auth check failed", ws_code=1011)
        # A device credential (Access service token) is download-only: GET /api/library/*
        # and nothing else, websockets included.
        if email and email.startswith("service:") and not (
                scope["type"] == "http" and scope.get("method") == "GET"
                and scope.get("path", "").startswith("/api/library/")):
            return await self._reject(scope, send, 403,
                                      "device credentials may only download the library",
                                      ws_code=1008)
        t1, t2 = current_tenant.set(ctx), current_principal.set(email)
        try:
            await self._app(scope, receive, send)
        finally:
            current_tenant.reset(t1)
            current_principal.reset(t2)

    @staticmethod
    async def _reject(scope, send, status: int, detail: str, *, ws_code: int) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": ws_code})
            return
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


_TRUE = {"1", "true", "yes", "on"}


def _env_flag(env, key: str) -> bool:
    """An opt-in boolean env flag: only 1/true/yes/on (any case) turn it on."""
    return env.get(key, "").strip().lower() in _TRUE


class FailedAttemptLimiter:
    """Brute-force brake for the shared db-viewer password, keyed by client address.

    In memory, per process: `max_failures` wrong passwords inside `window_s` lock that
    address out for `lockout_s`. While locked, EVERY attempt is refused (429), right
    password included — otherwise the lock would still answer "right" vs "wrong" and
    guessing would carry on at full speed. A correct password clears the address's
    failures. `clock` is injectable (monotonic seconds) so tests need no sleeping, and
    `reset()` forgets everything.

    Behind a tunnel or reverse proxy every request arrives from the same loopback
    address (`main` runs uvicorn with proxy_headers=False, so X-Forwarded-For is never
    the key), and the lock is then effectively global: a guesser locks the owner out
    too, which is the safe failure for a password that only gates owner-only tools."""

    _MAX_KEYS = 10_000          # bound memory under an address-spraying client

    def __init__(self, *, max_failures: int = 5, window_s: float = 300.0,
                 lockout_s: float = 900.0, clock=time.monotonic) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, key: str) -> "float | None":
        """Seconds until `key` may try again, or None when it is not locked out."""
        now = self._clock()
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return None
            if now >= until:
                del self._locked_until[key]
                return None
            return until - now

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            if key not in self._failures and len(self._failures) >= self._MAX_KEYS:
                self._prune(now)
            recent = [t for t in self._failures.get(key, ()) if now - t < self.window_s]
            recent.append(now)
            if len(recent) >= self.max_failures:
                self._locked_until[key] = now + self.lockout_s
                self._failures.pop(key, None)
            else:
                self._failures[key] = recent

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()

    def _prune(self, now: float) -> None:
        for k in [k for k, ts in self._failures.items()
                  if not ts or now - ts[-1] >= self.window_s]:
            del self._failures[k]
        for k in [k for k, until in self._locked_until.items() if now >= until]:
            del self._locked_until[k]
        if len(self._failures) >= self._MAX_KEYS:   # still full: drop the oldest half
            for k in sorted(self._failures, key=lambda k: self._failures[k][-1])[
                    : self._MAX_KEYS // 2]:
                del self._failures[k]


def _identity() -> str:
    """The acting identity stamped on human-in-the-browser writes: the verified Access
    email when there is one, else the deploy-wide KB_DASHBOARD_IDENTITY."""
    return current_principal.get() or _DASHBOARD_IDENTITY


def _agent_last_seen(pool) -> "str | None":
    """When this tenant's kb-agent last spoke to the gateway; None before it ever has,
    and on a database that predates the agent_state migration."""
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT last_seen_at FROM agent_state WHERE id = 1").fetchone()
    except psycopg.errors.UndefinedTable:
        return None
    return row["last_seen_at"].isoformat() if row and row["last_seen_at"] else None


def _same_origin_loopback(origin: str, site: "str | None") -> bool:
    """The dashboard's own write through `vite preview` on a loopback port.

    The preview proxy forwards the page's Origin (http://127.0.0.1:<KB_FRONTEND_PORT>)
    unrewritten, so without this every local click (retry-failed, curation, merges) would
    403 unless that port were listed in KB_DASHBOARD_ORIGINS. Allowed only when the
    browser ALSO says Sec-Fetch-Site: same-origin -- a page on another loopback port is
    same-site or cross-site, and a remote page can never carry a loopback Origin."""
    return site == "same-origin" and bool(_LOOPBACK_ORIGIN.match(origin))


async def _reject_cross_site_writes(request, call_next):
    """Block cross-site state-changing requests (CSRF).

    CORS is not a defence here: it governs whether a page may *read* a response, not
    whether the request runs. A "simple" POST (Content-Type: text/plain, no custom
    headers) skips preflight entirely, so any page the user visits could drive this API
    on localhost — reaching approve_merge (enumerable integer id), block_class,
    dismiss_concept, retire_node and others. Several of those are modelled as
    reversible=False, so the damage would outlive the tab.

    Two independent signals, either sufficient to reject:
      * Sec-Fetch-Site — set by the browser, unforgeable by page JS. cross-site and
        same-site are refused; same-origin and none (address-bar / non-browser) pass.
      * Origin — belt and braces for clients predating Sec-Fetch-Site. Checked only
        when present; browsers always send it on cross-origin requests.

    Non-browser callers (curl, httpx, TestClient) send neither header and are unaffected,
    which keeps the CLI and the test suite working.
    """
    if request.method in _UNSAFE_METHODS:
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        if site in ("cross-site", "same-site") or (
            origin is not None and origin not in _ALLOWED_ORIGINS
            and not _same_origin_loopback(origin, site)
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "cross-site write rejected"},
            )
    return await call_next(request)


class ChangeHub:
    """Bridges Postgres ``LISTEN kb_change`` to every connected WebSocket client.

    One dedicated async connection LISTENs; each NOTIFY (emitted by the 0008 triggers on
    COMMIT) is fanned out to all client queues. Cross-process by construction: it relays
    changes made by the ingest daemon, this API, or manual SQL alike. The listener
    reconnects with backoff so a dropped connection never silently kills the feed.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._clients: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        """Cancel the LISTEN loop and wait for it -- bounded. On Python < 3.12 one cancel
        can be swallowed: psycopg's notifies() waits through asyncio.wait_for, which drops
        a cancellation that lands just as a notification completes the inner wait, and the
        loop then listens forever, so a bare `await task` never returned and hung lifespan
        shutdown (reproducible under a NOTIFY flood on 3.11, not on 3.13). So
        re-cancel until the task ends, and abandon it loudly rather than hang shutdown."""
        task, self._task = self._task, None
        if task is None:
            return
        for _ in range(_STOP_ATTEMPTS):
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=_STOP_POLL_S)
            if done:
                if not task.cancelled():
                    task.exception()            # retrieve it: no "never retrieved" noise
                return
        log.warning("change hub listener ignored %d cancels; abandoning it", _STOP_ATTEMPTS)

    async def _listen(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {_CHANGE_CHANNEL}")
                    async for note in conn.notifies():
                        for q in list(self._clients):
                            with contextlib.suppress(asyncio.QueueFull):
                                q.put_nowait(note.payload)
            except asyncio.CancelledError:
                raise
            except Exception:               # connection dropped -> back off and reconnect
                await asyncio.sleep(1.0)

    def register(self) -> "asyncio.Queue[str]":
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._clients.add(q)
        return q

    def unregister(self, q: "asyncio.Queue[str]") -> None:
        self._clients.discard(q)


def _meta(node: Node) -> dict:
    return {
        "id": str(node.id), "path": node.path,
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "type": node.type, "slug": node.slug, "title": node.title, "tags": node.tags,
        "importance": node.importance,
        "importance_modifiers": node.importance_modifiers,  # per-modifier contribution breakdown
        "analysis_excluded": node.analysis_excluded,  # self-flagged off entity analysis (tree/graph dim it)
        "doc_kind": node.doc_kind,  # emergent document-kind facet (None until classified)
        "updated_at": node.updated_at.isoformat(),    # last write to the node — drives the graph timestamp
    }


def _mark_effective_exclusion(metas: list[dict], excluded_paths: list[str]) -> list[dict]:
    """Tag each meta with analysis_excluded_effective: True when the node itself OR an ancestor
    folder is self-flagged, matched by ltree prefix against the (small) flagged-path set. Lets the
    file-explorer icon and graph node dim a whole excluded subtree, not just the toggled folder.
    Mutates and returns the dicts."""
    for m in metas:
        p = m["path"]
        m["analysis_excluded_effective"] = any(
            p == e or p.startswith(e + ".") for e in excluded_paths
        )
    return metas


def _mark_processing(metas: list[dict], proc: dict) -> list[dict]:
    """Attach each node's active processing tasks (pending/processing) as a 'processing' list of
    {task, status} badges — empty when nothing is in flight. Mirrors _mark_effective_exclusion:
    a batched post-pass over already-built metas (proc comes from KnowledgeBase.processing_map)."""
    for m in metas:
        m["processing"] = proc.get(UUID(m["id"]), [])
    return metas


def _mark_content_changed(metas: list[dict], changed: dict) -> list[dict]:
    """Stamp content_changed_at: when the node's content hash last actually changed in the
    ingest log (pipeline re-upserts and delete/recreate churn don't move it — see
    KnowledgeBase.content_changed_map, which this consumes). Folders roll up the newest
    change in their subtree so active areas read as active; a node the log never saw
    carries None. Same batched post-pass shape as _mark_effective_exclusion.

    The folder rollup is one pass over `changed`, crediting each change to every proper
    ancestor (whole ltree labels), not a scan of `changed` per folder: that was
    folders x changes (tens of milliseconds per tree request on a modest vault)."""
    newest_below: dict = {}
    for path, at in changed.items():
        labels = path.split(".")
        for depth in range(1, len(labels)):
            ancestor = ".".join(labels[:depth])
            prev = newest_below.get(ancestor)
            if prev is None or at > prev:
                newest_below[ancestor] = at
    for m in metas:
        if m["type"] == "folder":
            t = newest_below.get(m["path"])
        else:
            t = changed.get(m["path"])
        m["content_changed_at"] = t.isoformat() if t else None
    return metas


def _full(node: Node) -> dict:
    # external_id is the source's own identifier — for file sources (e.g. obsidian) it's the
    # original file path, surfaced in the detail panel so a renamed node still shows its origin.
    return {**_meta(node), "content": node.content, "summary": node.summary,
            "uri": node.uri, "source": node.source, "external_id": node.external_id,
            "lifecycle": node.lifecycle,                    # active | noise | dead
            "entity_skip_reason": node.entity_skip_reason,
            "dead_at": node.dead_at.isoformat() if node.dead_at else None,
            "dead_reason": node.dead_reason}


def _excluded(node: Node) -> dict:
    """A 'shut off' memory for the Memories settings view: identity, why, and when. `reason` is
    the dead reason for a retired memory, else the noise reason."""
    return {**_meta(node), "summary": node.summary,
            "lifecycle": node.lifecycle,                    # 'noise' | 'dead'
            "reason": node.dead_reason if node.lifecycle == "dead" else node.entity_skip_reason,
            "dead_at": node.dead_at.isoformat() if node.dead_at else None}


def _is_entity_subtree(node: Node) -> bool:
    """True for entity nodes and the reserved `entities` root folder. The memory tree
    excludes these — entities live in their own graph view, not the folder structure."""
    return (node.source == ENTITY_SOURCE
            or node.path == ENTITY_ROOT
            or node.path.startswith(ENTITY_ROOT + "."))


def _event(row: dict) -> dict:
    return {
        "id": row["id"], "at": row["at"].isoformat(), "event": row["event"],
        "source": row["source"], "external_id": row["external_id"],
        "node_id": str(row["node_id"]), "path": row["path"],
        "content_hash": row["content_hash"], "detail": row["detail"],
    }


def _stuck(row: dict) -> dict:
    return {
        "source": row["source"], "external_id": row["external_id"],
        "node_id": str(row["node_id"]), "path": row["path"],
        "error": row["error"], "since": row["since"].isoformat(),
    }


def _class_dict(r: dict) -> dict:
    return {"name": r["name"], "status": r["status"], "aliases": r["aliases"],
            "members": r["members"], "instances": r["instances"], "voters": r["voters"],
            "description": r["description"], "description_locked": r["description_locked"],
            "pinned": r["pinned"]}


def create_app(
    kb_or_pools, *, dsn: str | None = None, db_viewer_password: str | None = None,
    registry: "Registry | None" = None, verifier=None, allow_relogin: bool = False,
    db_password_limiter: "FailedAttemptLimiter | None" = None,
) -> FastAPI:
    """One KnowledgeBase (single-tenant: every request is the owner, as before) or a
    TenantPools (each request is bound to the tenant its identity maps to). With a
    verifier the Access JWT is checked in-app; without one, pools mode serves
    KB_TENANT (default the owner) unauthenticated, like the local MCP listener — but only
    while the registry is owner-only: an enabled tenant with no verifier is
    DashboardAuthRequired here, and a 503 per request if a reload adds one later.

    `allow_relogin` turns on POST /api/auth/claude/relogin (off ⇒ 404); `main()` reads it
    from KB_DASHBOARD_ALLOW_RELOGIN. `db_password_limiter` throttles wrong db-viewer
    passwords (a fresh FailedAttemptLimiter when omitted; also on app.state)."""
    if isinstance(kb_or_pools, TenantPools):
        pools, fixed_ctx = kb_or_pools, None
        registry = registry or pools.registry
        if verifier is None and _has_enabled_tenants(registry):
            raise DashboardAuthRequired(
                f"{registry.path or 'the tenant registry'} has enabled tenants but "
                "KB_DASHBOARD_AUTH is not cf-access: every request would be served as the "
                "owner. Set KB_DASHBOARD_AUTH=cf-access, or disable the tenants first")
    else:
        pools = None
        fixed_ctx = TenantContext(tenant=Tenant(OWNER_SLUG, frozenset(), database="single"),
                                  pool=getattr(kb_or_pools, "_pool", None), kb=kb_or_pools)
    # Every route keeps calling kb.<method>; the proxy forwards to the request's tenant.
    kb = TenantKBProxy()

    def _owner_kb() -> KnowledgeBase:
        return fixed_ctx.kb if fixed_ctx is not None else pools.get(registry.owner).kb

    def _resolve(headers: dict) -> "tuple[TenantContext, str | None]":
        if fixed_ctx is not None:
            return fixed_ctx, None
        if verifier is None:
            if _has_enabled_tenants(registry):     # a reload added one after start
                raise _AuthFailure(503, "dashboard auth required")
            return pools.get_by_slug(default_tenant_slug()), None
        try:
            email = verifier.verify(token_from_headers(headers))
        except AuthRejected as exc:
            if exc.status == 503:
                raise _AuthFailure(503, "auth backend unavailable") from None
            raise _AuthFailure(401, "unauthorized") from None   # any other status: fail closed
        # A device's service token maps through `library_tokens`, a person through `emails`.
        t = (registry.by_library_token(email.removeprefix("service:"))
             if email.startswith("service:") else registry.by_email(email))
        if t is None:
            raise _AuthFailure(403, "no tenant for this identity")
        return pools.get(t), email

    # One LISTEN connection per tenant database, opened on that tenant's first WS client.
    # Without a dsn in single-KB mode (e.g. tests) the WS endpoint still exists but simply
    # carries no live notifications.
    hubs: dict[str, ChangeHub] = {}

    async def _hub_for(ctx: TenantContext) -> "ChangeHub | None":
        hub_dsn = dsn if fixed_ctx is not None else registry.dsn_for(ctx.tenant)
        if not hub_dsn:
            return None
        h = hubs.get(ctx.tenant.slug)
        if h is None:
            h = hubs[ctx.tenant.slug] = ChangeHub(hub_dsn)
            await h.start()
        return h

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            for h in list(hubs.values()):
                await h.stop()
            hubs.clear()

    # No /docs, /redoc or /openapi.json: the schema maps every route for whoever reaches
    # the port, and the SPA never reads it.
    app = FastAPI(title="kb-dashboard", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    # Added FIRST => innermost: compresses route bodies only. The tree/entities/concepts
    # payloads are 0.5-1.8 MB of JSON that gzip ~7x, and through a tunnel or remote link they
    # cross the network on every page load. Bodies under 1 KB and the WebSocket are untouched.
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[h.strip() for h in
                       os.environ.get("KB_API_ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOSTS).split(",")
                       if h.strip()],
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=list(_ALLOWED_ORIGINS),
        allow_methods=["*"], allow_headers=["*"],
    )
    # Registered last => runs first, so a cross-site write is refused before it can reach
    # a handler body. CORS alone would let the write execute and only hide the response.
    app.middleware("http")(_reject_cross_site_writes)
    # Added LAST => runs FIRST: no other layer (or route) sees a request whose identity
    # has not been verified and bound to a tenant.
    app.add_middleware(_TenantMiddleware, resolve=_resolve)

    def require_owner() -> None:
        if not require_tenant().tenant.is_owner:
            raise HTTPException(status_code=403, detail="owner only")

    curation = CurationService(kb)

    limiter = db_password_limiter if db_password_limiter is not None else FailedAttemptLimiter()
    app.state.db_password_limiter = limiter

    def require_db_password(
        request: Request,
        x_db_password: str | None = Header(default=None, alias="X-DB-Password"),
    ) -> None:
        """Gate the owner-only db-viewer / tool-toggle / relogin endpoints behind a shared
        password. It is a second factor, not authentication: without
        KB_DASHBOARD_AUTH=cf-access the API serves every request as the owner. Unconfigured
        -> 503 so it can never be accidentally open; missing/wrong header -> 401
        (constant-time compare); too many wrong passwords from one client address -> 429
        with Retry-After until the lockout ends (see FailedAttemptLimiter). A missing or
        empty header is not a guess and does not count toward the lockout."""
        if not db_viewer_password:
            raise HTTPException(status_code=503, detail="db viewer not configured")
        key = request.client.host if request.client else "unknown"
        wait = limiter.retry_after(key)
        if wait is not None:
            raise HTTPException(status_code=429, detail="too many failed db viewer attempts",
                                headers={"Retry-After": str(max(1, int(wait + 0.999)))})
        if not x_db_password:
            raise HTTPException(status_code=401, detail="invalid db viewer password")
        if not hmac.compare_digest(x_db_password.encode(), db_viewer_password.encode()):
            limiter.record_failure(key)
            raise HTTPException(status_code=401, detail="invalid db viewer password")
        limiter.record_success(key)

    def require_relogin_enabled() -> None:
        # Opt-in (KB_DASHBOARD_ALLOW_RELOGIN): off, the route answers as if it did not
        # exist. Checked BEFORE the password so a disabled route never acts as a
        # password oracle or burns lockout budget.
        if not allow_relogin:
            raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Readiness probe: DB reachable AND the inference server up. 200 when both pass,
        503 otherwise, with a per-check breakdown. Unauthenticated on purpose — it exposes
        no data and is meant to be polled by an uptime check. Failures answer with fixed
        words; the exception (which can name a DSN, host or path) goes to the log only."""
        import urllib.request
        checks: dict = {}
        ok = True
        try:
            _owner_kb().healthcheck()
            checks["db"] = "ok"
        except Exception:
            log.warning("healthz: db check failed", exc_info=True)
            checks["db"] = "error"
            ok = False
        inf_url = os.environ.get("KB_INFERENCE_URL", "http://127.0.0.1:8001")
        try:
            from kb_core.ingest.inference import inference_auth_headers
            req = urllib.request.Request(f"{inf_url}/health", headers=inference_auth_headers())
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    checks["inference"] = "ok"
                else:
                    checks["inference"] = f"status {resp.status}"
                    ok = False
        except Exception:
            log.warning("healthz: inference check failed", exc_info=True)
            checks["inference"] = "unreachable"
            ok = False
        return JSONResponse(checks, status_code=200 if ok else 503)

    @app.websocket("/api/ws")
    async def ws_changes(ws: WebSocket) -> None:
        # Push live change notifications to the UI. The client only listens; we also read
        # (and ignore) its frames purely to detect disconnect promptly.
        if not _ws_origin_allowed(ws.headers.get("origin")):
            await ws.close(code=1008)          # policy violation; refused before accept
            return
        hub = await _hub_for(require_tenant())
        await ws.accept()
        if hub is None:
            await ws.close()
            return
        q = hub.register()

        async def pump() -> None:
            while True:
                await ws.send_text(await q.get())

        async def watch() -> None:
            while True:
                await ws.receive_text()

        tasks = {asyncio.create_task(pump()), asyncio.create_task(watch())}
        try:
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            for t in tasks:
                t.cancel()
            hub.unregister(q)

    @app.exception_handler(LookupError)
    def _not_found(_request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    def _bad_validation(_request, exc: ValidationError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    def _bad_value(_request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(psycopg.errors.UniqueViolation)
    def _conflict(_request, exc: psycopg.errors.UniqueViolation):
        return JSONResponse(status_code=409, content={"detail": "slug already exists here"})

    @app.exception_handler(CurationConflict)
    def _curation_conflict(_request, exc: CurationConflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ConsolidationBusy)
    def _consolidation_busy(_request, exc: ConsolidationBusy):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # Skill registry errors carry a stable code (kb_core.skills.SkillError); the body is
    # the same "<code>: <message>" an agent sees over MCP.
    _SKILL_STATUS = {"not_found": 404, "revision_not_found": 404, "file_not_found": 404,
                     "invalid_argument": 422, "invalid_package": 422}

    @app.exception_handler(skills.SkillError)
    def _skill_error(_request, exc: skills.SkillError):
        return JSONResponse(status_code=_SKILL_STATUS.get(exc.code, 500),
                            content={"detail": str(exc)})

    @app.get("/api/tree")
    def get_tree() -> list[dict]:
        # Memories only — the entity subtree is served separately by /api/entities.
        metas = [_meta(n) for n in kb.list(limit=_TREE_LIMIT, with_content=False)
                 if not _is_entity_subtree(n)]
        metas = _mark_effective_exclusion(metas, kb.analysis_excluded_paths())
        metas = _mark_content_changed(metas, kb.content_changed_map())
        return _mark_processing(metas, kb.processing_map([UUID(m["id"]) for m in metas]))

    @app.get("/api/subtree")
    def get_subtree(path: str) -> dict:
        nodes = kb.subtree(path, with_content=False)
        present = {str(n.id) for n in nodes}
        links = [
            {"source": str(n.parent_id), "target": str(n.id), "kind": "containment"}
            for n in nodes
            if n.parent_id is not None and str(n.parent_id) in present
        ]
        # Effective exclusion is resolved against the FULL flagged set (an excluded ancestor can
        # sit above this subtree's root), so a subtree opened inside an excluded folder still dims.
        metas = _mark_effective_exclusion([_meta(n) for n in nodes], kb.analysis_excluded_paths())
        metas = _mark_content_changed(metas, kb.content_changed_map())
        metas = _mark_processing(metas, kb.processing_map([UUID(m["id"]) for m in metas]))
        return {"nodes": metas, "links": links}

    @app.get("/api/entities")
    def get_entities(include_blocked: bool = False) -> dict:
        # The isolated entity graph: the `entities` root hub, its classes, and instances.
        # Each node carries entity_type ('class'|'instance') + a role (root|class|instance) so
        # the UI can show classes by default and reveal instances on demand. An instance
        # is ONE canonical node and its category is a LINK, so class->instance edges come from
        # entity_class_links (not the tree); the flat `entities.instances` folder is omitted.
        # root->class is still tree containment (classes hang off the hub). Blocked classes are
        # hidden unless include_blocked.
        # Keep only the hub + the typed entity nodes (drop intermediate folders like
        # `entities.instances`, whose containment is replaced by class links).
        nodes = [n for n in kb.subtree(ENTITY_ROOT)
                 if n.path == ENTITY_ROOT or n.type == ENTITY_TYPE]
        ents = kb.list_entities()
        etype = {str(e.node.id): e.entity_type for e in ents}
        estatus = {str(e.node.id): e.status for e in ents}
        blocked_ids = {str(e.node.id) for e in ents
                       if e.entity_type == ENTITY_CLASS and e.status == STATUS_BLOCKED}
        if not include_blocked:
            nodes = [n for n in nodes if str(n.id) not in blocked_ids]
        present = {str(n.id) for n in nodes}

        def _role(n: Node) -> str:
            if n.type != ENTITY_TYPE:
                return "root"  # the reserved `entities` hub folder
            return "class" if etype.get(str(n.id)) == ENTITY_CLASS else "instance"

        out = [{**_meta(n), "entity_type": etype.get(str(n.id)),
                "status": estatus.get(str(n.id)), "role": _role(n)}
               for n in nodes]
        out = _mark_processing(out, kb.processing_map([UUID(o["id"]) for o in out]))
        # root -> class via tree containment (a class's parent is the entities hub)
        links = [
            {"source": str(n.parent_id), "target": str(n.id), "kind": "containment"}
            for n in nodes
            if n.parent_id is not None and str(n.parent_id) in present
        ]
        # class -> instance via the is_a membership links (the new category overlay)
        links += [
            {"source": str(cid), "target": str(iid), "kind": "containment"}
            for iid, cid in kb.entity_class_link_pairs()
            if str(cid) in present and str(iid) in present
        ]
        return {"nodes": out, "links": links}

    @app.get("/api/concepts")
    def get_concepts(activity_days: "int | None" = None) -> dict:
        # The derived concept layer (read-only): every active concept with its scored
        # members. One payload — graph shaping happens client-side (buildConceptGraph)
        # and the same data drives the concept card, so no per-concept detail endpoint.
        # activity_days adds a per-concept authorship-activity rollup (ingest-log based).
        return kb.concepts_view(activity_days=activity_days)

    @app.get("/api/concepts/run-status")
    def get_concept_run_status() -> dict:
        """Is the concept layer rebuilding, and what did the last rebuild do? Backs the
        dashboard's rebuild button. Cheap enough to call on every change-feed tick."""
        return kb.consolidation_run_status(run_timeout_s=_RUN_TIMEOUT_S)

    @app.post("/api/concepts/run")
    def request_concept_run() -> dict:
        """Rebuild the concept layer NOW instead of waiting for the churn trigger's quiet
        window. Fire-and-forget: kb-mcp-consolidated does the work (it owns every pass, so
        nothing can interleave) and the live change feed reflects progress. Idempotent only
        while a request sits unclaimed — clicking then returns current state, never a
        second pass. A click made while a pass is already RUNNING *is* queued and served by
        the next pass: the running pass already read its inputs and cannot reflect what the
        user just did. Deliberately bypasses the enrichment-drain gate, so the
        layer may be built over partially-enriched notes and the daemon will rebuild once
        writes go quiet; `backlog` in the response is what the UI shows to make that
        visible."""
        return kb.request_consolidation(source="dashboard")

    @app.get("/api/aging")
    def get_aging():
        """Content-age-decay master switch."""
        return {"enabled": kb.age_decay_enabled()}

    @app.post("/api/aging")
    def set_aging(payload: SetAgeDecayIn):
        """Origin-guarded like every dashboard write; ungated like /api/concepts/run."""
        return kb.set_age_decay(payload.enabled, updated_by=_identity())

    @app.get("/api/policy")
    def get_policy() -> dict:
        """The per-install policy this process loaded — read-only.
        `source` is the file path, or null for the shipped defaults; `overlays` lists
        the prompt files the overlay directory currently shadows."""
        pol = kb.policy
        return {"source": pol.source_path, "is_default": pol.is_default,
                "overlays": kb.prompt_overlays(), "policy": pol.to_dict()}

    @app.get("/api/concepts/constraints")
    def list_concept_constraints() -> dict:
        return {"constraints": [
            {**c, "left_id": str(c["left_id"]), "right_id": str(c["right_id"])}
            for c in kb.concept_constraints()]}

    @app.post("/api/concepts/constraints")
    def add_concept_constraint(body: dict) -> dict:
        try:
            row = kb.add_concept_constraint(body["kind"], body["left_id"],
                                            body["right_id"], note=body.get("note"))
        except psycopg.errors.ForeignKeyViolation:
            # An anchor this tenant does not have (another tenant's id, or none): the FK
            # refuses it; answer 404 without echoing the id.
            raise HTTPException(status_code=404, detail="unknown anchor id") from None
        return {**row, "left_id": str(row["left_id"]),
                "right_id": str(row["right_id"])}

    @app.delete("/api/concepts/constraints/{cid}")
    def delete_concept_constraint(cid: int) -> dict:
        return {"deleted": kb.delete_concept_constraint(cid)}

    @app.get("/api/concepts/separations")
    def list_concept_separations() -> dict:
        return {"separations": [
            {**s, "left_id": str(s["left_id"]), "right_id": str(s["right_id"])}
            for s in kb.concept_separations()]}

    @app.post("/api/concepts/separations")
    def add_concept_separation(body: dict) -> dict:
        row = kb.add_concept_separation(body["left_id"], body["right_id"],
                                        note=body.get("note"))
        return {**row, "left_id": str(row["left_id"]),
                "right_id": str(row["right_id"])}

    @app.delete("/api/concepts/separations/{sid}")
    def delete_concept_separation(sid: int) -> dict:
        return {"deleted": kb.delete_concept_separation(sid)}

    @app.get("/api/expand/{any_id}")
    def expand_node(any_id: UUID, k: int = 8) -> dict:
        # Read-only one-hop pivot around a memory, entity, or concept.
        return kb.expand(any_id, k=k)

    def _concept_out(row: dict) -> dict:
        return {**row, "id": str(row["id"])}

    @app.post("/api/concepts/curate/parse", dependencies=[Depends(require_owner)])
    def curate_parse(body: dict) -> dict:
        # NL -> structured curation ops, grounded in a server-built roster. Curation is
        # a WRITE path, so this fails closed: inference errors are a 503 (nothing is
        # written), and any op referencing an id outside the roster is demoted to a
        # warning even if the model emitted it. Nothing here mutates state — /apply does.
        text = str(body.get("text", "")).strip()
        if not text:
            raise ValueError("empty instruction")
        roster = kb.concept_curation_roster(text)
        concept_ids = {c["id"] for c in roster["concepts"]}
        entity_ids = {e["id"] for e in roster["entities"]}
        names = {c["id"]: c["name"] for c in roster["concepts"]}
        names.update({e["id"]: e["name"] for e in roster["entities"]})
        try:
            # Router build stays inside the try: a routing error is an inference
            # failure -> the same fail-closed 503, and the rest of the dashboard
            # never depends on inference config being valid.
            client = InferenceRouter(os.environ).client_for("curate_parse")
            sys_msg, usr_msg = build_curation_prompt(text, roster)
            parsed = parse_curation(client.complete(sys_msg, usr_msg,
                                                    schema=CURATION_SCHEMA))
        except Exception:
            return JSONResponse(status_code=503,
                                content={"detail": "inference unavailable"})
        ops, warnings = [], list(parsed["warnings"])
        for op in parsed["ops"]:
            # Type-aware grounding: concept-typed fields (concept_id and separate's
            # left/right_concept_id) must resolve in the concept roster;
            # left_id/right_id/node_id are entity-typed fields and must resolve in the
            # entity roster. A concept id smuggled into an entity field (or vice versa)
            # passes a same-set check but then blows up curate_apply's FK constraint —
            # so each field is checked against the roster of its own type.
            concept_refs = [op.get(k) for k in
                            ("concept_id", "left_concept_id", "right_concept_id")
                            if op.get(k) is not None]
            entity_refs = [op.get(k) for k in ("node_id", "left_id", "right_id")
                           if op.get(k) is not None]
            grounded = (all(str(r) in concept_ids for r in concept_refs)
                       and all(str(r) in entity_ids for r in entity_refs))
            if not grounded:
                warnings.append(f"dropped {op['op']}: reference outside the roster")
                continue
            ops.append(op)
        return {"ops": ops,
                "summary": [describe_op(op, names) for op in ops],
                "warnings": warnings}

    @app.post("/api/concepts/curate/apply", dependencies=[Depends(require_owner)])
    def curate_apply(body: dict) -> dict:
        applied, errors = [], []
        for op in body.get("ops", []):
            try:
                k = op["op"]
                if k == "rename":
                    kb.set_concept_label(UUID(op["concept_id"]), name=op["name"])
                elif k == "set_kind":
                    kb.set_concept_label(UUID(op["concept_id"]), kind=op["kind"])
                elif k == "set_description":
                    kb.set_concept_label(UUID(op["concept_id"]),
                                         description=op["description"])
                elif k == "dismiss":
                    kb.set_concept_status(UUID(op["concept_id"]), "dismissed")
                elif k == "restore":
                    kb.set_concept_status(UUID(op["concept_id"]), "active")
                elif k in ("must_link", "cannot_link"):
                    kb.add_concept_constraint(k, op["left_id"], op["right_id"],
                                              note=op.get("note"))
                elif k == "separate":
                    kb.add_concept_separation(UUID(op["left_concept_id"]),
                                              UUID(op["right_concept_id"]),
                                              note=op.get("note"))
                elif k in ("remove_member", "pin_member"):
                    kb.override_concept_member(UUID(op["concept_id"]),
                                               UUID(op["node_id"]),
                                               "remove" if k == "remove_member"
                                               else "pin", note=op.get("note"))
                else:
                    raise ValueError(f"unknown op: {k}")
                applied.append(op)
            except (KeyError, LookupError, ValueError, psycopg.IntegrityError) as exc:
                errors.append(f"{op.get('op', '?')}: {exc}")
        return {"applied": applied, "errors": errors}

    @app.post("/api/concepts/{concept_id}/label")
    def set_concept_label(concept_id: UUID, body: dict) -> dict:
        return _concept_out(kb.set_concept_label(
            concept_id, name=body.get("name"), kind=body.get("kind"),
            description=body.get("description"), lock=True))

    @app.post("/api/concepts/{concept_id}/dismiss")
    def dismiss_concept(concept_id: UUID) -> dict:
        return _concept_out(kb.set_concept_status(concept_id, "dismissed"))

    @app.post("/api/concepts/{concept_id}/restore")
    def restore_concept(concept_id: UUID) -> dict:
        return _concept_out(kb.set_concept_status(concept_id, "active"))

    @app.post("/api/concepts/{concept_id}/members/{node_id}/remove")
    def remove_concept_member(concept_id: UUID, node_id: UUID) -> dict:
        return kb.override_concept_member(concept_id, node_id, "remove")

    @app.post("/api/concepts/{concept_id}/members/{node_id}/pin")
    def pin_concept_member(concept_id: UUID, node_id: UUID) -> dict:
        return kb.override_concept_member(concept_id, node_id, "pin")

    @app.delete("/api/concepts/{concept_id}/members/{node_id}/override")
    def clear_concept_member_override(concept_id: UUID, node_id: UUID) -> dict:
        return {"deleted": kb.clear_concept_member_override(concept_id, node_id)}

    @app.get("/api/doc-kinds")
    def list_doc_kinds(all: bool = False) -> dict:
        # The emergent document-kind facet, support-ranked. Established kinds only by
        # default; ?all=true reveals the long tail (stored either way, just not shown).
        return {"kinds": kb.doc_kind_rollup(include_all=all)}

    @app.get("/api/doc-kinds/{kind}/memories")
    def doc_kind_memories(kind: str) -> dict:
        # Aggregate click-through: every live memory of one kind, tree order. The rollup
        # (doc_kind_rollup) counts live memories only, so this listing must match or counts
        # and click-through disagree -- filter out dead (retired) memories here.
        return {"memories": [_meta(n) for n in kb.list(kind=kind, limit=500)
                             if n.dead_at is None]}

    @app.get("/api/classes")
    def list_classes(include_blocked: bool = False) -> dict:
        # The class manager: every category with its status, member/instance/voter counts,
        # description, and lock state — the surface for curating the taxonomy.
        rows = kb.list_entity_classes(include_blocked=include_blocked)
        return {"classes": [_class_dict(r) for r in rows]}

    def _class_row(name: str) -> dict | None:
        norm = normalize_entity_name(name)
        for r in kb.list_entity_classes(include_blocked=True):
            if normalize_entity_name(r["name"]) == norm:
                return r
        return None

    @app.get("/api/classes/{name}")
    def get_class(name: str) -> dict:
        row = _class_row(name)
        if row is None:
            raise LookupError(f"class not found: {name}")
        return _class_dict(row)

    @app.patch("/api/classes/{name}")
    def patch_class(name: str, body: SetClassDescriptionIn) -> dict:
        if kb.set_class_description(name, body.description) is None:
            raise LookupError(f"class not found: {name}")
        return _class_dict(_class_row(name))

    @app.get("/api/classes/{name}/voters")
    def class_voters(name: str) -> dict:
        # The memories whose enrichment produced this class — shown in the class manager so
        # you can see exactly which notes feed (and would be re-enriched on pruning) it.
        return {"voters": [_meta(n) for n in kb.class_voters(name)]}

    @app.post("/api/classes/{name}/block")
    def block_class(name: str, body: BlockClassIn | None = None) -> dict:
        purge = body.purge_instances if body else True
        requeue = body.requeue_voters if body else True
        # Count the voters up front so the response can report how many notes were re-queued.
        requeued = len(kb.class_voters(name)) if requeue else 0
        ent = kb.block_class(name, purge_instances=purge, requeue_voters=requeue)
        if ent is None:
            raise LookupError(f"class not found: {name}")
        return {"name": ent.canonical_name, "status": ent.status, "requeued": requeued}

    @app.post("/api/classes/{name}/unblock")
    def unblock_class(name: str) -> dict:
        ent = kb.unblock_class(name)
        if ent is None:
            raise LookupError(f"class not found: {name}")
        return {"name": ent.canonical_name, "status": ent.status}

    @app.post("/api/classes/{name}/merge")
    def merge_class(name: str, body: MergeClassesIn) -> dict:
        # Fold `name` into body.target (also how a rename is done: merge into the new name).
        ent = kb.merge_classes(name, body.target)
        if ent is None:
            raise LookupError(f"class not found: {name}")
        return {"name": ent.canonical_name, "status": ent.status}

    @app.post("/api/entity/{entity_id}/reparent")
    def reparent_instance(entity_id: str, body: ReparentInstanceIn) -> dict:
        ent = kb.reparent_instance(UUID(entity_id), body.new_category)
        if ent is None:
            raise LookupError(f"instance not found: {entity_id}")
        # Category is a link now, so report the instance's class memberships (not entity_type,
        # which is the fixed 'instance' discriminator).
        return {**_meta(ent.node), "entity_type": ent.entity_type,
                "classes": [c.canonical_name for c in kb.classes_of(ent.node.id)]}

    @app.get("/api/ingest/log")
    def get_ingest_log(limit: int = 50, source: str | None = None,
                       event: str | None = None) -> dict:
        # event=enrichment_failed -> the failures view; detail carries the error reason.
        events = kb.ingest_log(limit=limit, source=source, event=event)
        return {"events": [_event(r) for r in events]}

    @app.post("/api/ingest/retry-failed")
    def retry_failed_ingest(body: RetryFailedIn | None = None) -> dict:
        # Refresh the failure pile: re-queue 'failed' enrichments as 'pending' so the
        # worker re-attempts them (e.g. after the LLM backend recovers). `source` scopes
        # broadly; `node_ids` scopes precisely to the retryable subset the dashboard picks.
        source = body.source if body else None
        ids = [UUID(x) for x in body.node_ids] if body and body.node_ids is not None else None
        return {"requeued": kb.retry_failed_enrichment(source=source, node_ids=ids)}

    @app.post("/api/ingest/stuck/dismiss")
    def dismiss_stuck_ingest(body: DismissStuckIn) -> dict:
        # Close a stuck record from the banner. For a still-failing pull record the next
        # tick re-opens it (honest); for a push failure, which never retries, this is how
        # it leaves the banner. Origin-guarded by the CSRF middleware like every write;
        # not password-gated (parity with retry-failed). ok reports whether a row was
        # actually written -- False for a record that was never stuck or already resolved.
        return {"ok": kb.log_ingest_resolved(body.source, body.external_id, reason="dismissed")}

    @app.get("/api/ingest/processing")
    def get_processing() -> dict:
        # Memories currently being enriched — the UI diffs this set on each live change to
        # surface a transient "began analysis" toast for a memory that just entered the worker.
        # Memories only: structural folders and the entity subtree aren't "analyzed" content.
        return {"nodes": [_meta(n) for n in kb.processing_nodes()
                          if n.type != "folder" and not _is_entity_subtree(n)]}

    @app.get("/api/me")
    def get_me() -> dict:
        ctx = require_tenant()
        return {"email": _identity(), "tenant": ctx.tenant.slug, "is_owner": ctx.tenant.is_owner}

    @app.get("/api/ingest/status")
    def get_ingest_status() -> dict:
        return {
            # When this tenant's kb-agent last reached the gateway (None: never / pre-0048).
            "agent_last_seen_at": _agent_last_seen(require_tenant().pool),
            "enrichment": kb.enrichment_status_counts(),
            # Active provider outages — the frontend banners these so a paused lane
            # reads as "waiting for 5:30am", not as a silently idle pipeline.
            "pauses": [
                {"provider": p["provider"], "until": p["until"].isoformat(),
                 "reason": p["reason"]}
                for p in kb.active_pauses()
            ],
            # Records the engine refused to store (pull or push) — one banner row each.
            # Zero rows per tick: the engine logs one ingest_failed per change, so this is
            # the set of currently-stuck records, not a failure count.
            "stuck": [_stuck(r) for r in kb.stuck_ingest_records()],
            "sources": [
                {
                    "source": s["source"],
                    "last_synced_at": s["last_synced_at"].isoformat(),
                    "cursor": s["cursor"],
                }
                for s in kb.sync_state()
            ],
        }

    @app.get("/api/retrieval/traces")
    def get_retrieval_traces(limit: int = 50, tool: str | None = None,
                             confidence: str | None = None,
                             judged: bool | None = None,
                             include_evals: bool = False) -> dict:
        rows = kb.trace_recent(limit=max(1, min(limit, 500)), tool=tool,
                               confidence=confidence, judged=judged,
                               include_evals=include_evals)
        for r in rows:
            r["at"] = r["at"].isoformat()
            r["request_id"] = str(r["request_id"])
            r["target_id"] = str(r["target_id"]) if r["target_id"] else None
        return {"traces": rows}

    @app.get("/api/retrieval/traces/summary")
    def get_retrieval_trace_summary(days: int = 7,
                                    include_evals: bool = False) -> dict:
        return kb.trace_summary(days=max(1, min(days, 365)),
                                include_evals=include_evals)

    @app.get("/api/retrieval/heatmap")
    def get_retrieval_heatmap(days: str = "30", bucket: str = "day", tz: str = "UTC",
                              path: "str | None" = None, limit: int = 200,
                              include_evals: bool = False) -> dict:
        """Per-memory surfaced/opened counts per day/week (Memories page heatmap).
        `days=all` is all recorded history; numeric windows clamp to 1..365."""
        if days == "all":
            window = None
        else:
            try:
                window = max(1, min(int(days), 365))
            except ValueError:
                raise HTTPException(status_code=422, detail="days must be an integer or 'all'")
        try:
            return kb.retrieval_heatmap(days=window, bucket=bucket, tz=tz,
                                        path=path or None, limit=limit,
                                        include_evals=include_evals)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    # Registered AFTER /traces/summary -- FastAPI matches routes in registration
    # order, so /summary must win before the int-typed {trace_id} path param
    # could otherwise swallow it; the int typing is a second guard (a request
    # for literal "summary" would 422 here rather than shadow the route above).
    @app.get("/api/retrieval/traces/{trace_id}")
    def get_retrieval_trace(trace_id: int) -> dict:
        row = kb.trace_get(trace_id)
        if row is None:
            raise LookupError(f"trace not found: {trace_id}")
        row["at"] = row["at"].isoformat()
        row["request_id"] = str(row["request_id"])
        row["target_id"] = str(row["target_id"]) if row["target_id"] else None
        for j in row["judgments"]:
            j["at"] = j["at"].isoformat()
        return row

    @app.get("/api/retrieval/traces/{trace_id}/replay")
    def get_retrieval_trace_replay(trace_id: int) -> dict:
        # Separate from the detail route so the drill-down never pays for the
        # replay payload (~5-15 KB). 404 covers unknown ids, non-search tools and
        # errored traces alike -- trace_replay returns None for all three.
        row = kb.trace_replay(trace_id)
        if row is None:
            raise LookupError(f"replay not available for trace {trace_id}")
        row["at"] = row["at"].isoformat()
        return row

    @app.post("/api/retrieval/traces/{trace_id}/judgment")
    def post_retrieval_trace_judgment(trace_id: int, body: dict) -> dict:
        # judged_by is fixed server-side: the verified principal (or the deploy
        # identity); scripted and LLM judges write through KnowledgeBase with their own.
        row = kb.add_judgment(trace_id, verdict=str(body.get("verdict", "")),
                              judged_by=_identity(), notes=body.get("notes"))
        row["at"] = row["at"].isoformat()
        return row

    # ---- turns: the calls one agent made for ONE user request ----------------
    # No /summary sibling under /turns, so the int-typed {turn_id} param needs no
    # registration-order care.

    def _iso_turn(row: dict) -> dict:
        row["started_at"] = row["started_at"].isoformat()
        row["ended_at"] = row["ended_at"].isoformat()
        return row

    @app.get("/api/retrieval/turns")
    def get_retrieval_turns(limit: int = 50, tool: str | None = None,
                            confidence: str | None = None,
                            judged: bool | None = None,
                            include_evals: bool = False) -> dict:
        rows = kb.trace_turns(limit=max(1, min(limit, 500)), tool=tool,
                              confidence=confidence, judged=judged,
                              include_evals=include_evals)
        return {"turns": [_iso_turn(r) for r in rows]}

    @app.get("/api/retrieval/turns/{turn_id}")
    def get_retrieval_turn(turn_id: int) -> dict:
        row = kb.trace_turn(turn_id)
        if row is None:
            raise LookupError(f"turn not found: {turn_id}")
        _iso_turn(row)
        for c in row["traces"]:
            c["at"] = c["at"].isoformat()
            c["request_id"] = str(c["request_id"])
            c["target_id"] = str(c["target_id"]) if c["target_id"] else None
        for j in row["judgments"]:
            j["at"] = j["at"].isoformat()
        return row

    @app.post("/api/retrieval/turns/{turn_id}/judgment")
    def post_retrieval_turn_judgment(turn_id: int, body: dict) -> dict:
        # judged_by fixed server-side, same rule as the per-call route.
        row = kb.add_turn_judgment(turn_id, verdict=str(body.get("verdict", "")),
                                   judged_by=_identity(), notes=body.get("notes"))
        row["at"] = row["at"].isoformat()
        return row

    @app.get("/api/retrieval/evals", dependencies=[Depends(require_owner)])
    def get_retrieval_evals() -> dict:
        return evals_reader.read_evals()

    def _tool_dict(row: dict) -> dict:
        return {"tool": row["tool"], "kind": row["kind"], "enabled": row["enabled"],
                "updated_at": row["updated_at"].isoformat(),
                "updated_by": row["updated_by"]}

    @app.get("/api/mcp/tools")
    def get_mcp_tools() -> dict:
        # Open like the other reads: it exposes tool names the MCP server already
        # advertises to any client that connects. Only the flip is gated.
        return {"tools": [_tool_dict(r) for r in kb.tool_states()]}

    @app.post("/api/mcp/tools/{tool}",
              dependencies=[Depends(require_owner), Depends(require_db_password)])
    def set_mcp_tool(tool: str, body: SetToolEnabledIn) -> dict:
        # updated_by is fixed server-side, same rule as the trace adjudication.
        row = kb.set_tool_enabled(tool, body.enabled, updated_by=_identity())
        if row is None:
            raise LookupError(f"tool not found: {tool}")
        return _tool_dict(row)

    # ---- Skills page: read-only over the skill registry ----------------------------
    # See docs/setup/recipes/skill-registry.md. No write route on
    # purpose -- skills change only through KB_MCP's upsert_skill / delete_skill. Not
    # owner-only: each tenant reads its own registry through the request-bound kb.

    @app.get("/api/skills")
    def get_skills(q: str | None = None, cursor: str | None = None,
                   archived: bool = False) -> dict:
        # The page lists items still waiting for review too (badged); agents never see them.
        return kb.search_skills(q, cursor, include_archived=archived, include_unapproved=True)

    @app.get("/api/skills/{name}")
    def get_skill_detail(name: str, revision: int | None = None) -> dict:
        return kb.get_skill(name, revision, default="latest")

    # `revision` is required, as in MCP's get_skill_file: the page passes the revision
    # it is showing, so one view never mixes files across an intervening update.
    @app.get("/api/skills/{name}/files/{path:path}")
    def get_skill_file_content(name: str, path: str, revision: int) -> dict:
        return kb.get_skill_file(name, path, revision)

    @app.get("/api/skills/{name}/usage")
    def get_skill_usage(name: str, days: int = 30, tz: str = "UTC") -> dict:
        return kb.skill_usage_for(name, days=days, tz=tz)

    # /api/skill-usage, not /api/skills/usage: `usage` is a valid skill name.
    @app.get("/api/skill-usage")
    def get_skill_library_usage(days: int = 30, tz: str = "UTC") -> dict:
        return kb.skill_usage(days=days, tz=tz)

    # ---- Agents: the same read-only view over the agent library -----------------
    @app.get("/api/agents")
    def get_agents(q: str | None = None, cursor: str | None = None,
                   archived: bool = False) -> dict:
        return kb.search_agents(q, cursor, include_archived=archived, include_unapproved=True)

    @app.get("/api/agents/{name}")
    def get_agent_detail(name: str, revision: int | None = None) -> dict:
        return kb.get_agent(name, revision, default="latest")

    # ---- Library download --------------------------------------------------------
    # What devices sync from: one gzip tar per kind of every active item's APPROVED
    # revision. The ETag is a content tag, so an unchanged library is one cheap 304.
    # Reachable by a device's Access service token (download-only, see _TenantMiddleware).

    def _library(kind: str, plural: str, if_none_match: str | None) -> Response:
        etag = kb.library_etag(kind)
        sent = {t.strip().removeprefix("W/").strip('"')
                for t in (if_none_match or "").split(",") if t.strip()}
        headers = {"ETag": f'"{etag}"', "Cache-Control": "no-cache"}
        if etag in sent:
            return Response(status_code=304, headers=headers)
        etag, body = kb.export_library(kind)
        headers["ETag"] = f'"{etag}"'
        headers["Content-Disposition"] = f'attachment; filename="{plural}.tar.gz"'
        return Response(content=body, media_type="application/gzip", headers=headers)

    @app.get("/api/library/skills")
    def get_library_skills(if_none_match: str | None = Header(default=None)) -> Response:
        return _library("skill", "skills", if_none_match)

    @app.get("/api/library/agents")
    def get_library_agents(if_none_match: str | None = Header(default=None)) -> Response:
        return _library("agent", "agents", if_none_match)

    @app.get("/api/library/client.py")
    def get_library_client() -> Response:
        """The kb-library client, byte for byte: how a fresh device bootstraps."""
        return Response(content=Path(library_client.__file__).read_bytes(),
                        media_type="text/x-python; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/api/auth/claude", dependencies=[Depends(require_owner)])
    def get_claude_auth() -> dict:
        """Health of the Claude credential the enrichment provider runs on.

        Returns status and dates only — never a token, in any field. `relogin_enabled`
        tells the banner whether the one-click re-auth route is switched on. With no
        task routed to claude-cli it answers `ok` / in_use=false without running `claude`,
        so an Ollama-only install never sees "enrichment is stranding"."""
        if not claude_cli_in_use():
            return {**NOT_IN_USE, "relogin_enabled": allow_relogin}
        return {**claude_auth_status(), "in_use": True, "relogin_enabled": allow_relogin}

    # Opt-in (KB_DASHBOARD_ALLOW_RELOGIN=1, off by default ⇒ 404) and password-gated
    # because it STARTS A PROCESS on the host (a macOS Terminal). It is safe to expose only
    # while it stays parameterless: the command below is a fixed literal and no request
    # data reaches the shell. Adding an argument turns this into a remote-command endpoint
    # and needs a different review.
    @app.post("/api/auth/claude/relogin",
              dependencies=[Depends(require_owner), Depends(require_relogin_enabled),
                            Depends(require_db_password)])
    def post_claude_relogin() -> dict:
        result = subprocess.run(["osascript", "-e",
                        'tell application "Terminal" to do script "claude auth login"'],
                       timeout=10)
        if result.returncode != 0:
            # Most commonly a launchd context lacking Terminal/AppleEvents TCC
            # consent (osascript exits -1743): nothing actually opened, so we
            # must not report success — the frontend's fallback message is the
            # correct experience here.
            raise HTTPException(status_code=502, detail="could not open Terminal")
        return {"launched": True}

    @app.get("/api/node/{node_id}")
    def get_node(node_id: str) -> dict:
        node = kb.get(UUID(node_id))
        if node is None:
            raise LookupError(f"node not found: {node_id}")
        return _detail(node)

    @app.get("/api/node/{node_id}/memories")
    def get_node_memories(node_id: str, limit: int = 25, offset: int = 0) -> dict:
        # One importance-ordered page of the memories an entity was found in (its
        # in-links) plus the total, so the panel can lazily "show more". Empty page for
        # non-entity nodes, which carry no entity_links. Bad paging params are clamped,
        # not rejected — the panel always wants a page back.
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        rows, total = kb.memories_linked_page(UUID(node_id), limit=limit, offset=offset)
        memories = []
        for n, lt, w in rows:
            event = n.event_at or n.source_created_at  # real-world date when known
            memories.append({**_meta(n), "link_type": lt, "weight": w,
                             "event_at": event.isoformat() if event else None})
        return {"memories": memories, "total": total}

    @app.get("/api/provenance")
    def get_provenance() -> dict:
        # Provenance view: every ACTIVE entity with the memories it was found in (its
        # entity_links in-links), each memory tagged class/instance by link_type. Answers
        # "what entities came from what memories", grouped by entity. Entities ordered by
        # type then name; memories strongest-first within each.
        return {"entities": [
            {**_meta(ent.node), "name": ent.canonical_name,
             "entity_type": ent.entity_type, "status": ent.status,
             "summary": ent.node.summary,
             "memories": [{**_meta(m), "summary": m.summary, "link_type": lt}
                          for m, lt in mems]}
            for ent, mems in kb.active_entity_provenance()
        ]}

    @app.get("/api/memories/excluded")
    def list_excluded(kind: str | None = None) -> dict:
        """The 'shut off' memories for the settings view — everything excluded from the
        going-forward entity system, labeled. kind in {None (both), 'noise', 'dead'}."""
        return {"memories": [_excluded(n) for n in kb.list_excluded(kind=kind)]}

    def _detail(node: Node) -> dict:
        """Node detail (`_full`) plus the analysis-exclusion and significance read models."""
        return {**_full(node), **kb.analysis_exclusion_view(node), **kb.significance_view(node)}

    def _reload(node_id: str) -> dict:
        node = kb.get(UUID(node_id))
        if node is None:
            raise LookupError(f"node not found: {node_id}")
        return _detail(node)

    @app.post("/api/node/{node_id}/retire")
    def retire_node(node_id: str, body: RetireIn | None = None) -> dict:
        """Manually retire a memory (mark it dead): freeze it, keep its past influence."""
        kb.mark_dead(UUID(node_id), reason=body.reason if body else None)
        return _reload(node_id)

    @app.post("/api/node/{node_id}/revive")
    def revive_node(node_id: str) -> dict:
        """Bring a retired memory back: active and crawlable again."""
        kb.revive(UUID(node_id))
        return _reload(node_id)

    @app.post("/api/node/{node_id}/analysis-exclude")
    def analysis_exclude(node_id: str, body: AnalysisExcludeIn | None = None) -> dict:
        """Exclude this node (a folder or a memory) from entity analysis. For a folder the whole
        subtree is excluded by inheritance. `retract` also pulls the subtree's past entity influence.
        Entity nodes are not excludable."""
        node = kb.get(UUID(node_id))
        if node is None:
            raise LookupError(f"node not found: {node_id}")
        if _is_entity_subtree(node):
            raise ValueError("entities cannot be excluded from analysis")
        updated = kb.set_analysis_excluded(
            UUID(node_id), True, retract=body.retract if body else False)
        return _detail(updated)

    @app.post("/api/node/{node_id}/analysis-include")
    def analysis_include(node_id: str) -> dict:
        """Re-include this node in entity analysis (clear its own exclusion flag)."""
        updated = kb.set_analysis_excluded(UUID(node_id), False)
        return _detail(updated)

    @app.post("/api/node/{node_id}/significance")
    def set_significance(node_id: str, body: SignificanceIn) -> dict:
        """Set a node's manual significance value. On a folder the value cascades (stacking) to
        every descendant; on a document it affects only that node. Returns the node with its
        refreshed factor breakdown."""
        updated = kb.set_importance(UUID(node_id), body.value)
        return _detail(updated)

    @app.post("/api/node/{node_id}/reanalyze")
    def reanalyze_node(node_id: str) -> dict:
        """Manually trigger a re-crawl of this memory: ask the re-analysis daemon
        (kb-mcp-reanalyzed) to re-run its LLM passes over the node now, rather than waiting
        for its timer. Fire-and-forget — the daemon does the work in the background and the
        live change feed reflects the result. Only memories are crawlable (not folders or
        entities); the daemon additionally skips nodes that aren't first-pass-enriched."""
        node = kb.get(UUID(node_id))
        if node is None:
            raise LookupError(f"node not found: {node_id}")
        if node.type == "folder" or _is_entity_subtree(node):
            raise ValueError("only memories can be re-crawled")
        kb.request_reanalysis(UUID(node_id))
        return {"requested": True, "node_id": node_id}

    @app.patch("/api/node/{node_id}")
    def patch_node(node_id: str, body: UpdateIn) -> dict:
        patch = NodePatch(**body.model_dump())
        return _full(kb.update(UUID(node_id), patch))

    @app.post("/api/node/{node_id}/move")
    def move_node(node_id: str, body: MoveIn) -> dict:
        return _full(kb.move(UUID(node_id), body.new_parent_path))

    @app.delete("/api/node/{node_id}")
    def delete_node(node_id: str, recursive: bool = False) -> dict:
        return {"deleted": kb.delete(UUID(node_id), recursive=recursive)}

    @app.post("/api/node")
    def create_node(body: CreateIn) -> dict:
        slug = body.slug
        if slug is None:
            if not body.title:
                raise ValueError("provide a slug or a title")
            slug = sanitize_slug(body.title)
        record = NodeRecord(
            source="agent", external_id=body.external_id or str(uuid4()),
            type=body.type, content=body.content, slug=slug,
            parent_path=body.parent_path, title=body.title,
            summary=body.summary, uri=body.uri, tags=body.tags or [],
        )
        return _full(kb.upsert(record))

    @app.get("/api/merge-suggestions")
    def list_merge_suggestions(kind: str | None = None) -> list[dict]:
        return kb.list_merge_suggestions(kind=kind)

    @app.post("/api/merge-suggestions/{cid}/approve")
    def approve_merge(cid: int, body: ApproveMergeIn | None = None) -> dict:
        tid = body.target_id if body else None
        survivor = kb.approve_merge(cid, UUID(tid) if tid else None)
        if survivor is None:
            raise LookupError("merge candidate not found or already resolved")
        return {"merged": True, "target_id": str(survivor.node.id)}

    @app.post("/api/merge-suggestions/{cid}/reject")
    def reject_merge(cid: int, body: RejectMergeIn | None = None) -> dict:
        if not kb.reject_merge(cid, reason=(body.reason if body else None)):
            raise LookupError("merge candidate not found or already resolved")
        return {"rejected": True}

    @app.get("/api/curation/commands")
    def curation_commands() -> dict:
        # Introspection that drives the UI: each action's label, args, and reversibility.
        return {"commands": curation.commands()}

    @app.post("/api/curation/{action}/preview", dependencies=[Depends(require_owner)])
    def curation_preview(action: str, body: dict) -> dict:
        # Read-only blast radius for a curation action; never mutates.
        return asdict(curation.preview(action, **body))

    @app.post("/api/curation/{action}", dependencies=[Depends(require_owner)])
    def curation_apply(action: str, body: dict) -> dict:
        return asdict(curation.apply(action, **body))

    # ---- raw Postgres table browser (password-gated; see require_db_password) ----
    @app.post("/api/db/auth", dependencies=[Depends(require_owner), Depends(require_db_password)])
    def db_auth() -> dict:
        # Lets the UI verify the password before revealing the browser; the gate is the
        # dependency, so reaching the body means the password was accepted.
        return {"ok": True}

    @app.get("/api/db/tables", dependencies=[Depends(require_owner), Depends(require_db_password)])
    def db_tables() -> dict:
        # Every non-system base table, 1:1 with Postgres, with column/estimated-row counts.
        return {"tables": kb.browse_tables()}

    @app.get("/api/db/table/{schema}/{name}",
             dependencies=[Depends(require_owner), Depends(require_db_password)])
    def db_table(schema: str, name: str, limit: int = db_browse.ROW_LIMIT_DEFAULT,
                 offset: int = 0) -> dict:
        # One page of a table's rows plus its columns, primary key, and exact total.
        limit = max(1, min(limit, db_browse.ROW_LIMIT_MAX))
        offset = max(0, offset)
        page = kb.browse_table_page(schema, name, limit=limit, offset=offset)
        if page is None:
            raise LookupError(f"table not found: {schema}.{name}")
        return {**page, "schema": schema, "name": name, "limit": limit, "offset": offset}

    return app


# KB_DASHBOARD_AUTH values. Unset/empty = no login (local use only); `none` = the same, but
# the operator states that another gate in front of the API verifies every request, which
# lets a non-local hostname start with a warning instead of a refusal.
_DASHBOARD_AUTH_MODES = ("cf-access", "none")


def _dashboard_auth_mode(env) -> str:
    """The normalised KB_DASHBOARD_AUTH value ("" when unset). Any other value raises
    ValueError: a typo such as `cf_access` must not silently run with no login."""
    mode = env.get("KB_DASHBOARD_AUTH", "").strip().lower()
    if mode and mode not in _DASHBOARD_AUTH_MODES:
        raise ValueError(f"unknown KB_DASHBOARD_AUTH {mode!r} (expected "
                         f"{' | '.join(_DASHBOARD_AUTH_MODES)}, or unset)")
    return mode


def _dashboard_verifier(env, *, key_for_token=None) -> "CfAccessVerifier | None":
    """The Access verifier `main()` serves with: None unless KB_DASHBOARD_AUTH=cf-access.
    It accepts EVERY email a valid token carries (the Access policy already decides who
    gets a JWT); mapping the email to a tenant is the middleware's job, so an identity in
    no tenant is the contracted 403, not the verifier's 401. The same holds for a device's
    service token (principal `service:<client-id>`, mapped through `library_tokens` and
    limited to GET /api/library/* by _TenantMiddleware)."""
    if env.get("KB_DASHBOARD_AUTH", "").strip().lower() != "cf-access":
        return None
    cfg = CfAccessConfig(team_domain=env["KB_CF_ACCESS_TEAM_DOMAIN"],
                         audience=env["KB_CF_ACCESS_AUD_DASHBOARD"], emails=frozenset())
    return CfAccessVerifier(cfg, key_for_token=key_for_token, allow=lambda _email: True,
                            allow_service=lambda _cid: True)


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}


def _exposure_problem(env) -> "str | None":
    """Why the API must not start as configured: the deploy names a non-local hostname (so
    it is presumably reachable from elsewhere) while the API serves every request as the
    owner. main() refuses on it, or only warns under the explicit KB_DASHBOARD_AUTH=none."""
    if env.get("KB_DASHBOARD_AUTH", "").strip().lower() == "cf-access":
        return None
    hosts = []
    for key in ("KB_DASHBOARD_ORIGINS", "KB_FRONTEND_ALLOWED_HOSTS", "KB_API_ALLOWED_HOSTS"):
        for item in env.get(key, "").split(","):
            item = item.strip()
            if not item:
                continue
            host = urlsplit(item).hostname if "://" in item else item.rsplit(":", 1)[0]
            if host and host.lower() not in _LOCAL_HOSTS:
                hosts.append(host)
    if not hosts:
        return None
    return (f"the dashboard is configured for {', '.join(sorted(set(hosts)))} but "
            "KB_DASHBOARD_AUTH is not cf-access, so the API would serve EVERY request as the "
            "owner with no login. Set KB_DASHBOARD_AUTH=cf-access (docs/exposure.md, "
            "'Exposing the dashboard'), or KB_DASHBOARD_AUTH=none if another login gate in "
            "front of the API verifies every request.")


# KB_DB_VIEWER_PASSWORD guards the raw table browser, the MCP tool switches and (opt-in)
# the endpoint that opens a Terminal on the host: a one-guess value must not unlock them.
DB_VIEWER_MIN_LEN = 16
_DB_VIEWER_PLACEHOLDERS = {"change-me", "changeme", "password", "secret", "admin", "letmein"}
_DB_VIEWER_PLACEHOLDER_PREFIXES = ("change-me", "changeme", "replace-me", "replaceme", "your-")


def db_viewer_password_problem(value: "str | None") -> "str | None":
    """Why KB_DB_VIEWER_PASSWORD may not be used, or None if it may (or is simply unset:
    unset already keeps the gate locked)."""
    if value is None or not value.strip():
        return None
    low = value.strip().lower()
    hint = "generate one with: openssl rand -hex 16"
    if (low in _DB_VIEWER_PLACEHOLDERS or low.startswith(_DB_VIEWER_PLACEHOLDER_PREFIXES)
            or "<" in low or ">" in low):
        return f"KB_DB_VIEWER_PASSWORD is a placeholder value; {hint}"
    if len(value) < DB_VIEWER_MIN_LEN:
        return (f"KB_DB_VIEWER_PASSWORD is shorter than {DB_VIEWER_MIN_LEN} characters; "
                f"{hint}")
    if len(set(value)) < 4:
        return f"KB_DB_VIEWER_PASSWORD has almost no entropy; {hint}"
    return None


def _db_viewer_password(env) -> "str | None":
    """The password main() hands create_app. A weak one is REFUSED, not used: the gated
    endpoints stay locked (503, exactly as when it is unset) and the rest of the dashboard
    keeps working, so a weak value can never be the thing standing in front of them."""
    value = env.get("KB_DB_VIEWER_PASSWORD")
    problem = db_viewer_password_problem(value)
    if problem is not None:
        print(f"[dashboard] {problem}. The database browser, MCP tool switches and "
              "Claude relogin stay locked (503) until it is fixed.", file=sys.stderr,
              flush=True)
        return None
    return value


def main() -> None:
    """Entry point: load the tenant registry, serve the dashboard API over HTTP. Tenant
    pools open (and migrate) lazily on each tenant's first request. With
    KB_DASHBOARD_AUTH=cf-access every request must carry a valid Access JWT whose email
    the registry maps to a tenant; otherwise the API serves KB_TENANT (default owner)."""
    import uvicorn

    try:
        mode = _dashboard_auth_mode(os.environ)
    except ValueError as exc:
        print(f"refusing to start the dashboard API: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    if (problem := _exposure_problem(os.environ)) is not None:
        if mode != "none":
            print(f"refusing to start the dashboard API: {problem}", file=sys.stderr,
                  flush=True)
            raise SystemExit(1)
        print(f"WARNING: {problem} Starting anyway: KB_DASHBOARD_AUTH=none says another "
              "gate verifies every request.", file=sys.stderr, flush=True)
    registry = load_registry()
    pools = TenantPools(registry)
    verifier = _dashboard_verifier(os.environ)
    try:
        app = create_app(pools, registry=registry, verifier=verifier,
                         db_viewer_password=_db_viewer_password(os.environ),
                         allow_relogin=_env_flag(os.environ, "KB_DASHBOARD_ALLOW_RELOGIN"))
    except DashboardAuthRequired as exc:
        pools.close()
        print(f"refusing to start the dashboard API: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    # proxy_headers=False: the tunnel and vite reach us from loopback, and uvicorn would
    # otherwise take the client address from their X-Forwarded-For, so any caller could
    # pick the key FailedAttemptLimiter locks out.
    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("KB_DASHBOARD_PORT", "8000")), proxy_headers=False)


if __name__ == "__main__":
    main()
