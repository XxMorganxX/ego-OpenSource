"""Cloudflare Access JWT verification, shared by the dashboard and (via re-export) the
MCP listener. RS256 against the team JWKS, audience == the Access app's tag, issuer ==
the team domain, email in
the allowlist (or accepted by `allow`). A JWKS outage is 503 (fail closed, recover on the
next fetch); every other failure is 401 with a neutral body."""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import jwt as _jwt

log = logging.getLogger("kb-core.cf-access")


class AuthRejected(Exception):
    """A request that must not reach the app. status is 401 or 503; reason is for the
    server log only, never the response body."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class CfAccessConfig:
    team_domain: str
    audience: str
    emails: frozenset[str]


class CfAccessVerifier:
    """The one Access verifier (dashboard and the public MCP listener). RS256 signature
    against the team JWKS, audience == the Access app tag, issuer == the team domain,
    `exp`/`iat`/`iss` required, email claim lowercased. key_for_token(raw_jwt) -> verification key; the default PyJWKClient
    caches the JWKS for an hour and refetches once when it sees an unknown kid.
    allow(email) -> bool, when given, replaces the frozen cfg.emails allowlist (a tenant
    registry that reloads drives it; cfg.emails may then be empty). An empty email claim
    is passed to allow like any other value and is rejected unless allow accepts it.
    allow_service(client_id) -> bool, when given, accepts a service-token JWT (no email;
    `common_name` = the token's Client ID) as principal `service:<client-id>`. Without it a
    service token is rejected exactly as before (the MCP listener passes none). An email
    principal can never take the `service:` form, so the two kinds cannot be confused."""

    def __init__(self, cfg: CfAccessConfig, key_for_token: Callable[[str], object] | None = None,
                 allow: Callable[[str], bool] | None = None,
                 allow_service: Callable[[str], bool] | None = None) -> None:
        self._cfg = cfg
        self._allow = allow
        self._allow_service = allow_service
        if key_for_token is None:
            client = _jwt.PyJWKClient(f"https://{cfg.team_domain}/cdn-cgi/access/certs",
                                      cache_keys=True, lifespan=3600)

            def _default_key_for_token(tok: str) -> object:
                return client.get_signing_key_from_jwt(tok).key
            key_for_token = _default_key_for_token
        self._key_for_token = key_for_token

    def verify(self, token: str | None) -> str:
        if not token:
            raise AuthRejected(401, "missing Access token")
        try:
            key = self._key_for_token(token)
        except _jwt.exceptions.PyJWKClientConnectionError as exc:
            log.warning("Access JWKS unavailable (%s): rejecting with 503", exc)
            raise AuthRejected(503, f"jwks unavailable: {exc}") from exc
        except Exception as exc:  # unknown kid, garbage header, ... -- attacker-suppliable
            raise AuthRejected(401, f"cannot resolve signing key: {exc}") from exc
        try:
            payload = _jwt.decode(token, key, algorithms=["RS256"], audience=self._cfg.audience,
                                  issuer=f"https://{self._cfg.team_domain}",
                                  options={"require": ["exp", "iat", "iss"]})
        except _jwt.InvalidTokenError as exc:
            raise AuthRejected(401, f"invalid token: {exc}") from exc
        email = (payload.get("email") or "").lower()
        cid = payload.get("common_name")
        if not email and isinstance(cid, str) and cid and self._allow_service is not None:
            if self._allow_service(cid):
                return f"service:{cid}"
            raise AuthRejected(401, f"service token not allowlisted: {cid}")
        if email.startswith("service:"):
            # Reserved for service principals: an email claim spelled like one must never
            # be routed as a device credential (or a device as a person).
            raise AuthRejected(401, f"email claim uses the reserved service: prefix: {email}")
        ok = self._allow(email) if self._allow is not None else (email in self._cfg.emails)
        if not ok:
            raise AuthRejected(401, f"email not allowlisted: {email or '<none>'}")
        return email


def token_from_headers(headers: Mapping[str, str]) -> str | None:
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    if h.get("cf-access-jwt-assertion"):
        return h["cf-access-jwt-assertion"]
    for part in h.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "CF_Authorization" and value:
            return value
    return None
