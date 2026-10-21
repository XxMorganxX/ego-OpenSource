"""Auth wall for the PUBLIC MCP listener (com.kb.mcp-public, :3100).

The local listener (:3000) never loads this: auth_from_env() returns None when
KB_MCP_AUTH is unset and main() serves exactly as before. When KB_MCP_AUTH is
set, EVERY http request must authenticate — there are no exempt paths, because
cloudflared dials the origin from localhost and source-IP checks are useless.
Modes:
  cf-access       — validate the Cf-Access-Jwt-Assertion JWT Cloudflare Access
                    attaches after its managed-OAuth flow (signature via the
                    team's JWKS, audience tag, email allowlist).
  internal-secret — constant-time check of X-KB-MCP-Internal, injected by the
                    Worker OAuth proxy (the fallback deployment).
Misconfiguration exits at startup (loud, never a silent fallback). Auth
*failures* are 401 with a neutral body; a JWKS outage
is 503 — fail closed, stay up, recover on the next fetch.
"""

import hmac
import ipaddress
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import anyio

from kb_core.cf_access import AuthRejected, CfAccessConfig, CfAccessVerifier  # noqa: F401 — re-exported

log = logging.getLogger("kb-mcp.auth")


@dataclass(frozen=True)
class InternalSecretConfig:
    secret: str


AuthConfig = CfAccessConfig | InternalSecretConfig


def auth_from_env(env: Mapping[str, str]) -> AuthConfig | None:
    """Resolve the auth mode from process env; SystemExit with instructions on
    misconfiguration. None = no auth (the local listener's mode)."""
    mode = env.get("KB_MCP_AUTH", "").strip().lower()
    if not mode:
        return None
    if mode == "cf-access":
        team = env.get("KB_CF_ACCESS_TEAM_DOMAIN", "").strip()
        aud = env.get("KB_CF_ACCESS_AUD", "").strip()
        emails = frozenset(
            e.strip().lower()
            for e in env.get("KB_CF_ACCESS_EMAILS", "").split(",") if e.strip())
        missing = [n for n, v in (("KB_CF_ACCESS_TEAM_DOMAIN", team),
                                  ("KB_CF_ACCESS_AUD", aud),
                                  ("KB_CF_ACCESS_EMAILS", emails)) if not v]
        if missing:
            raise SystemExit(
                "KB_MCP_AUTH=cf-access requires " + ", ".join(missing)
                + " (team domain like myteam.cloudflareaccess.com; AUD is the "
                  "Access application's audience tag; EMAILS is a comma-separated "
                  "allowlist). Set them in KB_Core/.env.")
        return CfAccessConfig(team_domain=team, audience=aud, emails=emails)
    if mode == "internal-secret":
        secret = env.get("KB_MCP_INTERNAL_SECRET", "").strip()
        if len(secret) < 32:
            raise SystemExit(
                "KB_MCP_AUTH=internal-secret requires KB_MCP_INTERNAL_SECRET "
                "(>=32 chars; generate with: openssl rand -hex 32). "
                "Set it in KB_Core/.env and as a Worker secret.")
        return InternalSecretConfig(secret=secret)
    raise SystemExit(
        f"unknown KB_MCP_AUTH mode {mode!r} (expected cf-access | internal-secret)")


def refuse_internal_secret_with_tenants(cfg: AuthConfig | None, registry) -> None:
    """internal-secret carries no identity, so every request is served as the owner. With
    enabled tenants that would hand any secret holder the owner's data under a tenant's
    connection: SystemExit, as the dashboard refuses the same case."""
    if isinstance(cfg, InternalSecretConfig) and registry.tenants(include_owner=False):
        raise SystemExit(
            f"KB_MCP_AUTH=internal-secret serves every request as the owner, but "
            f"{getattr(registry, 'path', None) or 'the tenant registry'} has enabled "
            "tenants. Use KB_MCP_AUTH=cf-access, or disable the tenants first.")


def ensure_http_transport(transport: str, cfg: AuthConfig | None) -> None:
    """Auth applies to the HTTP listener only; auth+stdio is a misconfiguration
    (it would silently serve an UNAUTHENTICATED stdio surface while the operator
    believes auth is on)."""
    if cfg is not None and transport != "streamable-http":
        raise SystemExit(
            f"KB_MCP_AUTH is set but KB_MCP_TRANSPORT resolves to {transport!r}; "
            "the auth wall only exists on the streamable-http listener. "
            "Set KB_MCP_TRANSPORT=http or unset KB_MCP_AUTH.")


def _is_loopback(host: str) -> bool:
    h = host.strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False                      # a hostname other than localhost: not loopback


def bind_host_from_env(env: Mapping[str, str], cfg: AuthConfig | None,
                       transport: str) -> str:
    """KB_MCP_HOST (default 127.0.0.1), refused when it would serve the UNAUTHENTICATED
    tool surface off this machine. Without KB_MCP_AUTH the listener has no auth at all,
    and FastMCP turns its DNS-rebinding/Origin guard on only for loopback binds, so a
    non-loopback bind used to expose every tool (delete_memory, sync_source prune, ...)
    to the network with no check whatsoever. Mirrors KB_Inference's non-loopback refusal.
    stdio never binds, so it is never refused."""
    host = (env.get("KB_MCP_HOST") or "127.0.0.1").strip()
    if cfg is None and transport != "stdio" and not _is_loopback(host):
        raise SystemExit(
            f"refusing to bind the unauthenticated MCP listener to {host!r}: without "
            "KB_MCP_AUTH every tool would be open to the network. Keep KB_MCP_HOST on "
            "loopback (127.0.0.1) and reach it remotely through the authenticated "
            "listener (kb-svc.sh mcp-public: KB_MCP_PUBLIC_AUTH=cf-access | "
            "internal-secret behind a tunnel), or set KB_MCP_AUTH on this one.")
    return host


def public_hostname_from_env(env: Mapping[str, str], cfg: AuthConfig | None) -> str | None:
    """The public hostname tunnel traffic arrives with, or None. It is only honoured
    on an AUTHENTICATED listener: a public hostname on the unauthenticated local
    listener would let a tunnel pointed at the wrong port pass FastMCP's Host check
    and serve the bare tool surface to the internet. That combination is a
    misconfiguration, so it exits loudly at startup instead of being ignored."""
    hostname = env.get("KB_MCP_PUBLIC_HOSTNAME", "").strip()
    if not hostname:
        return None
    if cfg is None:
        raise SystemExit(
            "KB_MCP_PUBLIC_HOSTNAME is set but KB_MCP_AUTH is not: refusing to serve "
            "a public hostname from an unauthenticated listener. Only the public "
            "listener (kb-svc.sh mcp-public, which maps KB_MCP_PUBLIC_AUTH to "
            "KB_MCP_AUTH) may carry a public hostname. Unset KB_MCP_PUBLIC_HOSTNAME "
            "for the local listener, or set KB_MCP_AUTH=cf-access | internal-secret.")
    return hostname


_BODIES = {401: b"unauthorized", 500: b"internal error", 503: b"auth backend unavailable"}


async def _deny(send, status: int) -> None:
    body = _BODIES[status]
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class _AuthMiddleware:
    """Pure-ASGI wall. check(headers) raises AuthRejected or returns the verified
    principal (the email in cf-access mode, None in internal-secret mode), which
    is stored as scope["state"]["kb_principal"] before the app sees the request.
    It runs in a worker thread: the cf-access check can do a (rare, cached)
    blocking JWKS fetch, and the event loop must keep serving other sessions
    meanwhile."""

    def __init__(self, app, check: Callable[[dict[bytes, bytes]], str | None]):
        self._app = app
        self._check = check

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self._app(scope, receive, send)
        if scope["type"] != "http":
            # The wall allowlists scope types; anything unanticipated is refused,
            # never forwarded. uvicorn[standard] ships websockets, so :3100 WILL
            # negotiate upgrades from the network -- close the handshake unaccepted
            # (1008 = policy violation). The tool surface is http-only.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        try:
            principal = await anyio.to_thread.run_sync(self._check, headers)
        except AuthRejected as rej:
            ip = headers.get(b"cf-connecting-ip", b"?").decode("latin-1")
            log.warning("denied %s %s from %s: %s", rej.status,
                        scope.get("path", "?"), ip, rej.reason)
            return await _deny(send, rej.status)
        except Exception:  # the wall fails CLOSED — never let a crash skip or leak
            log.exception("auth check crashed; failing closed")
            return await _deny(send, 500)
        scope.setdefault("state", {})["kb_principal"] = principal
        await self._app(scope, receive, send)


def wrap_with_auth(app, cfg: AuthConfig, key_for_token=None, *,
                   allow: Callable[[str], bool] | None = None):
    """The one entrypoint main() uses for the public listener. allow(email), when
    given, replaces cfg.emails as the cf-access allowlist (ignored in
    internal-secret mode, which carries no identity)."""
    if isinstance(cfg, CfAccessConfig):
        verifier = CfAccessVerifier(cfg, key_for_token=key_for_token, allow=allow)

        def check(headers: dict[bytes, bytes]) -> str | None:
            raw = headers.get(b"cf-access-jwt-assertion", b"").decode("latin-1")
            return verifier.verify(raw or None)
    else:
        secret = cfg.secret.encode()

        def check(headers: dict[bytes, bytes]) -> str | None:
            if not hmac.compare_digest(headers.get(b"x-kb-mcp-internal", b""), secret):
                raise AuthRejected(401, "bad or missing X-KB-MCP-Internal")
            return None
    return _AuthMiddleware(app, check)
