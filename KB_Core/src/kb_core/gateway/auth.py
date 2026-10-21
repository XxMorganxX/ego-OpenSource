"""HMAC request authentication for the agent gateway.

    signature = hex(HMAC-SHA256(agent_secret,
                    "{ts}\n{nonce}\n{METHOD}\n{path}\n{query}\n{sha256_hex(body)}"))

`query` is the raw query string exactly as sent, without the leading "?" ("" when there is
none), so the parameters of a GET are signed along with its path. `nonce` is a fresh random
token per request (X-KB-Nonce); a nonce already accepted for a tenant inside the skew window
is refused, so a captured request cannot be replayed even within that window.

Verification runs in two steps so that the body is never read for a request that cannot
authenticate: `precheck` validates the headers (known enabled tenant, fresh timestamp,
well-formed nonce and signature) before the body is read; `check` then verifies the HMAC
over the body and burns the nonce. The tenant named in the request must be the tenant whose
key signed it. Failures carry a reason for the server log only; the client always sees a
neutral 401.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Hashable, Mapping
from dataclasses import dataclass

from kb_core.tenants import Registry, Tenant

MAX_SKEW_S = 300
HDR_TENANT = "x-kb-tenant"
HDR_TS = "x-kb-timestamp"
HDR_NONCE = "x-kb-nonce"
HDR_SIG = "x-kb-signature"
HDR_VERSION = "x-kb-agent-version"
HDR_AGENT_ID = "x-kb-agent-id"

_NONCE_RE = re.compile(r"[0-9A-Za-z_-]{16,128}")
_SIG_RE = re.compile(r"[0-9a-f]{64}")
REPLAY_CACHE_MAX = 100_000            # per tenant at the gateway; whole receiver at /ingest


class GatewayAuthError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def canonical(ts: str, nonce: str, method: str, path: str, query: str, body: bytes) -> bytes:
    return (f"{ts}\n{nonce}\n{method.upper()}\n{path}\n{query}\n"
            f"{hashlib.sha256(body).hexdigest()}").encode()


def sign(secret: str, ts: str, nonce: str, method: str, path: str, query: str, body: bytes) -> str:
    return hmac.new(secret.encode(), canonical(ts, nonce, method, path, query, body),
                    hashlib.sha256).hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(16)


def signed_headers(secret: str, tenant: str, method: str, path: str, body: bytes, *,
                   query: str = "", ts: int | None = None, nonce: str | None = None,
                   version: str = "test") -> dict[str, str]:
    ts_s = str(int(time.time()) if ts is None else int(ts))
    nonce = new_nonce() if nonce is None else nonce
    return {"X-KB-Tenant": tenant, "X-KB-Timestamp": ts_s, "X-KB-Nonce": nonce,
            "X-KB-Signature": sign(secret, ts_s, nonce, method, path, query, body),
            "X-KB-Agent-Version": version, "Content-Type": "application/json"}


class ReplayCache:
    """Keys (nonces, signatures) accepted within the skew window. An entry expires once its
    timestamp falls outside the window, after which the skew check refuses the request
    anyway. Bounded: past `max_entries` the oldest entries are dropped, which needs more than
    `max_entries` authenticated requests inside one window."""

    def __init__(self, max_entries: int = REPLAY_CACHE_MAX) -> None:
        self._seen: OrderedDict[Hashable, float] = OrderedDict()
        self._max = max_entries

    def __len__(self) -> int:
        return len(self._seen)

    def seen(self, key: Hashable, now: float) -> bool:
        while self._seen:
            oldest, expires = next(iter(self._seen.items()))
            if expires >= now:
                break
            del self._seen[oldest]
        return key in self._seen

    def add(self, key: Hashable, ts: int) -> None:
        self._seen[key] = ts + MAX_SKEW_S
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)

    def discard(self, key: Hashable) -> None:
        """Release a reservation whose request was not accepted after all."""
        self._seen.pop(key, None)


@dataclass(frozen=True)
class Claim:
    """A request whose headers passed `precheck`: who it claims to be, not yet proven."""
    tenant: Tenant
    ts: str
    ts_i: int
    nonce: str
    signature: str


def _headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def precheck(registry: Registry, headers: Mapping[str, str], *, now: float | None = None) -> Claim:
    """Everything that can be checked without the body. Cheap, and run before the body is
    read, so a request with an unknown tenant, a stale timestamp or a malformed nonce or
    signature never makes the gateway buffer its body."""
    h = _headers(headers)
    slug = h.get(HDR_TENANT, "")
    tenant = registry.by_slug(slug) if slug else None
    if tenant is None or tenant.is_owner or not tenant.enabled or not tenant.agent_secret:
        raise GatewayAuthError(f"unknown or disabled tenant {slug!r}")
    ts = h.get(HDR_TS, "")
    try:
        ts_i = int(ts)
    except ValueError:
        raise GatewayAuthError("bad timestamp") from None
    now = time.time() if now is None else now
    if abs(now - ts_i) > MAX_SKEW_S:
        raise GatewayAuthError(f"timestamp skew {abs(now - ts_i):.0f}s")
    nonce = h.get(HDR_NONCE, "")
    if not _NONCE_RE.fullmatch(nonce):
        raise GatewayAuthError("missing or bad nonce")
    sig = h.get(HDR_SIG, "")
    if not _SIG_RE.fullmatch(sig):
        raise GatewayAuthError("bad signature")
    return Claim(tenant, ts, ts_i, nonce, sig)


def check(claim: Claim, method: str, path: str, query: str, body: bytes, *,
          replay: dict[str, ReplayCache] | None, now: float | None = None) -> Tenant:
    """Verify the HMAC over the body, then refuse a nonce this tenant already used inside the
    window. The nonce is burned only after the signature verifies, so nothing
    unauthenticated can grow the cache. `replay` maps tenant slug to that tenant's cache
    (one tenant cannot evict another's nonces); None skips the replay check (tests only)."""
    expected = sign(claim.tenant.agent_secret, claim.ts, claim.nonce, method, path, query, body)
    # Compare bytes: compare_digest raises TypeError on non-ASCII str, and headers arrive latin-1.
    if not hmac.compare_digest(expected.encode(), claim.signature.encode("utf-8", "replace")):
        raise GatewayAuthError("bad signature")
    if replay is not None:
        cache = replay.setdefault(claim.tenant.slug, ReplayCache())
        now = time.time() if now is None else now
        if cache.seen(claim.nonce, now):
            raise GatewayAuthError("replayed nonce")
        cache.add(claim.nonce, claim.ts_i)
    return claim.tenant


def verify(registry: Registry, headers: Mapping[str, str], method: str, path: str,
           body: bytes, *, query: str = "", replay: dict[str, ReplayCache] | None = None,
           now: float | None = None) -> Tenant:
    """`precheck` + `check` in one call, for callers that already hold the body."""
    claim = precheck(registry, headers, now=now)
    return check(claim, method, path, query, body, replay=replay, now=now)
