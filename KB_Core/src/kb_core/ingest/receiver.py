"""The push receiver: an HMAC-authenticated endpoint that only durably stages events.

Every `POST /ingest/{source}` must carry:

    X-KB-Timestamp: <unix seconds>
    X-KB-Signature: hex(HMAC-SHA256(KB_INGEST_SECRET,
                        "{ts}\\n{METHOD}\\n{path}\\n{sha256_hex(body)}"))

This is the webhook scheme: simpler than the agent gateway's (`kb_core.gateway.auth`, which
also signs a per-request nonce and the query string), so a shell sender can produce it with
openssl. The route takes no query parameters, and the signature itself serves as the nonce.

The path (so the target source) and the body hash are both signed. The timestamp must be
within MAX_SKEW_S of the server clock, and a signature already accepted inside that window
is refused, so a captured request cannot be replayed. The timestamp is checked before the
body is read; the body is capped at MAX_BODY before it is read in full, parsed or
authenticated. Auth failures are logged with their reason; the client always sees a
neutral 401.

`/healthz` is the only unauthenticated route. There is no /docs, /openapi.json or log view.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from kb_core.gateway.auth import HDR_SIG, HDR_TS, MAX_SKEW_S, ReplayCache
from kb_core.ingest.inbox import Inbox

log = logging.getLogger("kb-ingest-receiver")

_VALID_EVENTS = {"upsert", "delete"}
MAX_BODY = 8 * 1024 * 1024
MIN_SECRET_LEN = 32

# Values that have appeared in example env files or docs. A key printed in a repo is not
# a secret, however long it is.
_PLACEHOLDERS = {"dev-secret", "secret", "test-secret", "changeme", "change-me", "password"}
_PLACEHOLDER_PREFIXES = ("change-me", "changeme", "replace-me", "replaceme", "your-", "dev-secret")


class InsecureSecretError(ValueError):
    """KB_INGEST_SECRET is unset, too short, or a known placeholder."""


def require_secret(value: str | None) -> str:
    """Return the ingest HMAC key, or refuse to run with one anyone could guess."""
    hint = "generate one with: openssl rand -hex 32"
    if value is None or not value.strip():
        raise InsecureSecretError(f"KB_INGEST_SECRET is unset or empty; {hint}")
    low = value.strip().lower()
    if (low in _PLACEHOLDERS or low.startswith(_PLACEHOLDER_PREFIXES)
            or "<" in low or ">" in low):
        raise InsecureSecretError(f"KB_INGEST_SECRET is a placeholder value; {hint}")
    if len(value) < MIN_SECRET_LEN:
        raise InsecureSecretError(
            f"KB_INGEST_SECRET is shorter than {MIN_SECRET_LEN} characters; {hint}")
    if len(set(value)) < 4:
        raise InsecureSecretError(f"KB_INGEST_SECRET has almost no entropy; {hint}")
    return value


def sign(secret: str, ts: str, method: str, path: str, body: bytes) -> str:
    """The webhook signature (see the module docstring)."""
    canonical = f"{ts}\n{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}".encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def signed_headers(secret: str, path: str, body: bytes, *, ts: int | None = None,
                   method: str = "POST") -> dict[str, str]:
    """Headers a sender attaches to `body` posted to `path` (e.g. "/ingest/webhook")."""
    ts_s = str(int(time.time()) if ts is None else int(ts))
    return {"X-KB-Timestamp": ts_s, "X-KB-Signature": sign(secret, ts_s, method, path, body),
            "Content-Type": "application/json"}


def _too_large() -> HTTPException:
    return HTTPException(status_code=413, detail="body too large")


async def _read_capped(request: Request, max_body: int) -> bytes:
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


def build_receiver(inbox: Inbox, *, secret: str, max_body: int = MAX_BODY) -> FastAPI:
    key = require_secret(secret)
    replay = ReplayCache()
    app = FastAPI(title="kb-mcp-ingest", docs_url=None, redoc_url=None, openapi_url=None)

    def _deny(request: Request, reason: str) -> HTTPException:
        log.warning("denied %s %s: %s", request.method, request.url.path, reason)
        return HTTPException(status_code=401, detail="unauthorized")

    def _fresh_timestamp(request: Request) -> tuple[str, int]:
        """Checked before the body is read: a stale or missing timestamp costs no body read."""
        ts = request.headers.get(HDR_TS, "")
        try:
            ts_i = int(ts)
        except ValueError:
            raise _deny(request, "missing or bad timestamp") from None
        skew = abs(time.time() - ts_i)
        if skew > MAX_SKEW_S:
            raise _deny(request, f"timestamp skew {skew:.0f}s")
        return ts, ts_i

    def _authenticate(request: Request, ts: str, ts_i: int, raw: bytes) -> bytes:
        """Verify the signature and RESERVE it in the replay cache in one synchronous step
        (no await in between), so a second copy arriving while the first is still staging
        is refused. The caller releases the reservation if the request is not accepted."""
        # Compare bytes: compare_digest raises TypeError on non-ASCII str.
        got = request.headers.get(HDR_SIG, "").encode("utf-8", "replace")
        expected = sign(key, ts, request.method, request.url.path, raw).encode()
        if not hmac.compare_digest(expected, got):
            raise _deny(request, "bad signature")
        if replay.seen(expected, time.time()):
            raise _deny(request, "replayed signature")
        replay.add(expected, ts_i)
        return expected

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/ingest/{source}", status_code=202)
    async def ingest(source: str, request: Request) -> dict:
        ts, ts_i = _fresh_timestamp(request)
        raw = await _read_capped(request, max_body)
        sig = _authenticate(request, ts, ts_i, raw)
        # Only a staged request burns its signature: a 400 below or a failed inbox write
        # releases the reservation so the sender is free to retry, and nothing
        # unauthenticated can grow the cache.
        try:
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid JSON") from None
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="body must be a JSON object")
            event_type = body.get("event_type", "upsert")
            if event_type not in _VALID_EVENTS:
                raise HTTPException(status_code=400, detail="bad event_type")
            # Blocking psycopg INSERT -> threadpool, so the event loop stays free.
            inbox_id = await run_in_threadpool(
                inbox.append, source, external_id=body.get("external_id"),
                event_type=event_type, payload=body.get("payload", {}),
            )
        except BaseException:
            replay.discard(sig)
            raise
        return {"queued": inbox_id}

    return app
