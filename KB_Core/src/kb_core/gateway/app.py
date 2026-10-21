"""Agent gateway endpoints. Every route depends on `authed`, which checks the signed
headers before reading the body, then verifies the per-tenant HMAC over the method, path,
query and raw body, burns the request's nonce, and binds that tenant's pool. Blocking
psycopg work runs in the threadpool so the long-poll never stalls another tenant's request."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from starlette.concurrency import run_in_threadpool

from kb_core import llm_jobs
from kb_core.gateway.auth import HDR_AGENT_ID, HDR_VERSION, GatewayAuthError, ReplayCache, check, precheck
from kb_core.gateway.limits import TokenBucket
from kb_core.ingest.inbox import Inbox
from kb_core.tenancy import TenantContext, TenantPools
from kb_core.tenants import Registry, load_registry

log = logging.getLogger("kb-gateway")

MAX_EVENTS = 100
MAX_BODY = 8 * 1024 * 1024
# GET routes take no body: one that arrives with a body is refused before it is buffered.
BODYLESS_METHODS = frozenset({"GET", "HEAD"})
MAX_TEXT = 1024 * 1024
MAX_WAIT_S = 30.0
MAX_CLAIM = 16
MAX_INFLIGHT_PER_TENANT = 4          # = the tenant pool's max_size (KB_TENANT_POOL_MAX default)
MAX_JOB_ID = 2**63 - 1
TOUCH_INTERVAL_S = 30.0
SOURCE = "vault"
# The agent's opaque per-install id and version are bookkeeping only (agent_state); anything
# that does not look like one is dropped rather than stored.
_AGENT_ID_RE = re.compile(r"[0-9A-Za-z_.+-]{1,64}")


def _agent_field(value: str | None) -> str | None:
    return value if value is not None and _AGENT_ID_RE.fullmatch(value) else None


def _too_large() -> HTTPException:
    return HTTPException(status_code=413, detail="body too large")


async def _read_capped(request: Request, max_body: int = MAX_BODY) -> bytes:
    """The raw body, refused with 413 as soon as it passes max_body — before it is parsed
    or authenticated, and without buffering a chunked upload that never declared a length."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > max_body:
        raise _too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_body:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _json(raw: bytes) -> dict:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return body


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_iso(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _parse_iso(value)
    except ValueError:
        return False
    return True


def _validate_event(ev) -> tuple[str, str, dict]:
    if not isinstance(ev, dict):
        raise ValueError("event must be an object")
    kind = ev.get("type")
    ext = ev.get("external_id")
    if ext is not None and not isinstance(ext, str):
        raise ValueError("external_id must be a string")
    if not ext:
        raise ValueError("external_id required")
    if kind == "delete":
        return "delete", ext, {"external_id": ext}
    if kind != "upsert":
        raise ValueError("type must be upsert or delete")
    rel, text, mtime, birth = ev.get("rel_path"), ev.get("text"), ev.get("mtime"), ev.get("birthtime")
    if (not isinstance(rel, str) or not rel.lower().endswith(".md") or rel.startswith("/")
            or ".." in rel.replace("\\", "/").split("/")):
        raise ValueError("rel_path must be a relative .md path")
    if not isinstance(text, str):
        raise ValueError("text required")
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise ValueError("text over 1 MiB")
    if not isinstance(mtime, str):
        raise ValueError("mtime required (ISO 8601)")
    if not _is_iso(mtime):
        raise ValueError("mtime must be ISO 8601")
    if birth is not None and not _is_iso(birth):
        raise ValueError("birthtime must be ISO 8601")
    return "upsert", ext, {"external_id": ext, "rel_path": rel, "text": text,
                           "mtime": mtime, "birthtime": birth}


def _job_dict(j: llm_jobs.Job) -> dict:
    # `deadline` (UTC ISO) and `deadline_in_s` (seconds left at claim, server clock -- immune
    # to the agent's clock skew) let the agent size its run to the producer's window.
    return {"id": j.id, "task": j.task, "system_prompt": j.system_prompt,
            "user_prompt": j.user_prompt, "schema": j.schema, "model_hint": j.model_hint,
            "claim_token": j.claim_token,
            "deadline": (j.deadline.astimezone(timezone.utc).isoformat()
                         if j.deadline is not None else None),
            "deadline_in_s": (round(j.remaining_s, 3) if j.remaining_s is not None else None)}


def create_gateway(pools: TenantPools, registry: Registry, *, rate_per_minute: int = 120,
                   poll_s: float = 1.0) -> FastAPI:
    # No /docs, /redoc or /openapi.json: the gateway is the tenants' only network surface and
    # serves nothing unauthenticated.
    app = FastAPI(title="kb-agent-gateway", docs_url=None, redoc_url=None, openapi_url=None)
    buckets: dict[str, TokenBucket] = {}
    inflight: dict[str, asyncio.Semaphore] = {}
    last_touch: dict[str, float] = {}
    replay: dict[str, ReplayCache] = {}     # per tenant: one tenant cannot evict another's nonces

    def _touch(ctx: TenantContext, version, agent_id) -> None:
        with ctx.pool.connection() as conn:
            llm_jobs.touch_agent(conn, version=version, install_id=agent_id)

    async def _heartbeat(request: Request, ctx: TenantContext) -> None:
        """agent_state is bookkeeping: a failed write is logged and retried on the next
        request, never allowed to fail the request that carried it."""
        slug = ctx.tenant.slug
        if time.monotonic() - last_touch.get(slug, -1e9) <= TOUCH_INTERVAL_S:
            return
        try:
            await run_in_threadpool(_touch, ctx, _agent_field(request.headers.get(HDR_VERSION)),
                                    _agent_field(request.headers.get(HDR_AGENT_ID)))
        except Exception as exc:
            log.warning("heartbeat failed tenant=%s: %s", slug, exc)
            return
        last_touch[slug] = time.monotonic()

    def _deny(request: Request, exc: GatewayAuthError) -> HTTPException:
        log.warning("denied %s %s: %s", request.method, request.url.path, exc.reason)
        return HTTPException(status_code=401, detail="unauthorized")

    async def authed(request: Request) -> AsyncIterator[TenantContext]:
        # Headers first: an unknown tenant, a stale timestamp or a malformed nonce or
        # signature is refused before a byte of the body is read.
        try:
            claim = precheck(registry, request.headers)
        except GatewayAuthError as exc:
            raise _deny(request, exc) from None
        cap = 0 if request.method.upper() in BODYLESS_METHODS else MAX_BODY
        body = await _read_capped(request, cap)
        request.state.raw_body = body           # routes parse this; the stream is consumed
        try:
            # url.query is the raw query string as sent (no "?"); the agent signs exactly that.
            tenant = check(claim, request.method, request.url.path, request.url.query, body,
                           replay=replay)
        except GatewayAuthError as exc:
            raise _deny(request, exc) from None
        # The threadpool is one shared 40-thread limiter and a tenant pool blocks getconn
        # when exhausted, so a tenant gets at most MAX_INFLIGHT_PER_TENANT requests in
        # flight; the rest are refused at once, before touching a pool or a thread. The
        # slot is held for the whole request, long-poll sleeps included.
        sem = inflight.setdefault(tenant.slug, asyncio.Semaphore(MAX_INFLIGHT_PER_TENANT))
        if sem.locked():
            log.warning("in-flight cap tenant=%s", tenant.slug)
            raise HTTPException(status_code=429, detail="too many concurrent requests",
                                headers={"Retry-After": "1"})
        await sem.acquire()                     # never suspends: the semaphore is not locked
        try:
            ctx = await run_in_threadpool(pools.get, tenant)
            await _heartbeat(request, ctx)
            yield ctx
        finally:
            sem.release()

    def _limited(ctx: TenantContext) -> None:
        bucket = buckets.setdefault(ctx.tenant.slug, TokenBucket(rate_per_minute, 60.0))
        if not bucket.take():
            log.warning("rate limited tenant=%s", ctx.tenant.slug)
            raise HTTPException(status_code=429, detail="rate limited")

    @app.post("/v1/notes", status_code=202)
    async def post_notes(request: Request, ctx: TenantContext = Depends(authed)) -> dict:
        _limited(ctx)
        events = _json(request.state.raw_body).get("events")
        if not isinstance(events, list) or len(events) > MAX_EVENTS:
            raise HTTPException(status_code=400, detail=f"events must be a list of at most {MAX_EVENTS}")
        accepted: list[tuple[str, str, dict]] = []
        rejected: list[dict] = []
        for ev in events:
            try:
                accepted.append(_validate_event(ev))
            except ValueError as exc:
                ext = ev.get("external_id") if isinstance(ev, dict) else None
                rejected.append({"external_id": ext if isinstance(ext, str) else "", "reason": str(exc)})

        def _stage() -> None:
            inbox = Inbox(ctx.pool)
            for kind, ext, payload in accepted:
                inbox.append(SOURCE, external_id=ext, event_type=kind, payload=payload)
        await run_in_threadpool(_stage)
        log.info("notes tenant=%s queued=%d rejected=%d", ctx.tenant.slug, len(accepted), len(rejected))
        return {"queued": len(accepted), "rejected": rejected}

    @app.post("/v1/manifest", status_code=202)
    async def post_manifest(request: Request, ctx: TenantContext = Depends(authed)) -> dict:
        _limited(ctx)
        body = _json(request.state.raw_body)
        ids, gen = body.get("ids"), body.get("generation")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(status_code=400, detail="ids must be a list of strings")
        if isinstance(gen, bool) or not isinstance(gen, int):
            raise HTTPException(status_code=400, detail="generation must be an integer")
        if gen < 0:
            raise HTTPException(status_code=400, detail="generation must be a non-negative integer")
        payload = {"ids": ids, "generation": gen, "allow_empty": bool(body.get("allow_empty", False))}
        await run_in_threadpool(lambda: Inbox(ctx.pool).append(
            SOURCE, external_id=None, event_type="manifest", payload=payload))
        log.info("manifest tenant=%s generation=%d ids=%d", ctx.tenant.slug, gen, len(ids))
        return {}

    def _claim(ctx: TenantContext, n: int) -> list[llm_jobs.Job]:
        with ctx.pool.connection() as conn:
            # A crashed agent's claim returns to the queue after the agent's own run timeout
            # plus a margin: inside the producer's window, so the
            # reclaimed job can still be answered. 2x the producer timeout never was.
            llm_jobs.reclaim_stale(conn, older_than_s=llm_jobs.STALE_CLAIM_S)
            return llm_jobs.claim(conn, max_jobs=n, worker=ctx.tenant.slug)

    @app.get("/v1/jobs")
    async def get_jobs(request: Request, ctx: TenantContext = Depends(authed),
                       max: int = Query(4, ge=1, le=MAX_CLAIM),
                       wait: float = Query(25.0, ge=0.0, le=MAX_WAIT_S)) -> dict:
        deadline = time.monotonic() + wait
        while True:
            jobs = await run_in_threadpool(_claim, ctx, max)
            if jobs or time.monotonic() >= deadline:
                return {"jobs": [_job_dict(j) for j in jobs]}
            await asyncio.sleep(poll_s)
            # An agent that hung up mid-poll must not have a job claimed in its name: that
            # job would sit unseen until the stale reclaim.
            if await request.is_disconnected():
                return {"jobs": []}

    @app.post("/v1/jobs/{job_id}")
    async def post_job(request: Request, job_id: int = Path(ge=1, le=MAX_JOB_ID),
                       ctx: TenantContext = Depends(authed)) -> dict:
        body = _json(request.state.raw_body)
        token = str(body.get("claim_token") or "")
        status = body.get("status")
        if status not in ("done", "failed"):
            raise HTTPException(status_code=400, detail="status must be done or failed")
        result = body.get("result")
        if status == "done" and not isinstance(result, str):
            raise HTTPException(status_code=400, detail="result must be a string")
        ra = body.get("retry_after")
        ra_dt = None
        if status == "failed" and ra is not None:
            if not _is_iso(ra):
                raise HTTPException(status_code=400, detail="retry_after must be ISO 8601")
            ra_dt = _parse_iso(ra)
            if ra_dt.tzinfo is None:
                ra_dt = ra_dt.replace(tzinfo=timezone.utc)

        def _apply() -> bool:
            with ctx.pool.connection() as conn:
                if status == "done":
                    return llm_jobs.complete(conn, job_id, token, result)
                return llm_jobs.fail(conn, job_id, token, str(body.get("error") or "agent failure"),
                                     retry_after=ra_dt)
        if not await run_in_threadpool(_apply):
            log.info("stale claim tenant=%s job=%d", ctx.tenant.slug, job_id)
            raise HTTPException(status_code=409, detail="stale claim")
        return {}

    @app.get("/v1/status")
    async def get_status(ctx: TenantContext = Depends(authed)) -> dict:
        def _read():
            with ctx.pool.connection() as conn:
                counts = llm_jobs.open_counts(conn)
                pending = conn.execute(
                    "SELECT count(*) AS n FROM ingest_inbox WHERE source = %s AND status = 'pending'",
                    (SOURCE,)).fetchone()["n"]
                seen = llm_jobs.agent_last_seen(conn)
            return counts, int(pending), seen
        counts, pending, seen = await run_in_threadpool(_read)
        return {"tenant": ctx.tenant.slug, "open_jobs": counts, "inbox_pending": pending,
                "agent_last_seen_at": seen.isoformat() if seen else None,
                "server_time": datetime.now(timezone.utc).isoformat()}

    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    registry = load_registry()
    pools = TenantPools(registry)
    try:
        uvicorn.run(create_gateway(pools, registry),
                    host=os.environ.get("KB_GATEWAY_HOST", "127.0.0.1"),
                    port=int(os.environ.get("KB_GATEWAY_PORT", "8040")), log_level="info",
                    proxy_headers=False)  # never take the client address from X-Forwarded-For
    finally:
        pools.close()


if __name__ == "__main__":
    main()
