"""Request signing — the client half of kb_core.gateway.auth (reimplemented on purpose:
the agent must not import the gateway package). Known-answer vectors shared by
KB_Agent/tests/test_signing.py and KB_Core/tests/test_gateway_auth.py pin the two
implementations together.

    signature = hex(HMAC-SHA256(secret,
                    "{ts}\\n{nonce}\\n{METHOD}\\n{path}\\n{query}\\n{sha256_hex(body)}"))

`query` is the raw query string as sent, without the "?"; `nonce` is fresh per request, and
the gateway accepts each one once."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from kb_agent import __version__
from kb_agent.config import AgentConfig


def sign(secret: str, ts: str, nonce: str, method: str, path: str, query: str, body: bytes) -> str:
    canonical = (f"{ts}\n{nonce}\n{method.upper()}\n{path}\n{query}\n"
                 f"{hashlib.sha256(body).hexdigest()}").encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def signed_headers(cfg: AgentConfig, method: str, path: str, body: bytes, *, query: str = "",
                   ts: int | None = None, nonce: str | None = None) -> dict[str, str]:
    ts_s = str(int(time.time()) if ts is None else int(ts))
    nonce = secrets.token_hex(16) if nonce is None else nonce
    h = {"X-KB-Tenant": cfg.tenant, "X-KB-Timestamp": ts_s, "X-KB-Nonce": nonce,
         "X-KB-Signature": sign(cfg.secret, ts_s, nonce, method, path, query, body),
         # An opaque random id made at install (config.install_id), never the hostname.
         "X-KB-Agent-Version": __version__, "X-KB-Agent-Id": cfg.install_id,
         "Content-Type": "application/json"}
    if cfg.cf_client_id and cfg.cf_client_secret:
        h["CF-Access-Client-Id"] = cfg.cf_client_id
        h["CF-Access-Client-Secret"] = cfg.cf_client_secret
    return h
