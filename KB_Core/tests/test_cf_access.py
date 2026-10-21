import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from kb_core import cf_access as CF

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUB = KEY.public_key()
CFG = CF.CfAccessConfig(team_domain="t.cloudflareaccess.com", audience="aud-1",
                        emails=frozenset({"alice@example.com"}))


def _tok(email="alice@example.com", aud="aud-1", exp_delta=600,
         iss="https://t.cloudflareaccess.com"):
    now = int(time.time())
    claims = {"email": email, "aud": aud, "iat": now, "exp": now + exp_delta}
    if iss is not None:
        claims["iss"] = iss
    return jwt.encode(claims, KEY, algorithm="RS256")


def _verifier(allow=None):
    return CF.CfAccessVerifier(CFG, key_for_token=lambda tok: PUB, allow=allow)


def test_valid_token_returns_the_email():
    assert _verifier().verify(_tok()) == "alice@example.com"


@pytest.mark.parametrize("tok, status", [
    (None, 401), ("garbage", 401), (_tok(aud="other"), 401), (_tok(exp_delta=-5), 401),
    (_tok(email="bob@example.com"), 401),
    (_tok(iss="https://other.cloudflareaccess.com"), 401), (_tok(iss=None), 401),
])
def test_bad_tokens_are_401(tok, status):
    with pytest.raises(CF.AuthRejected) as exc:
        _verifier().verify(tok)
    assert exc.value.status == status


def test_allow_callback_overrides_the_frozen_list():
    assert _verifier(allow=lambda e: e == "bob@example.com").verify(_tok(email="bob@example.com")) == "bob@example.com"
    with pytest.raises(CF.AuthRejected):
        _verifier(allow=lambda e: False).verify(_tok())


def test_token_from_headers_prefers_the_header_then_the_cookie():
    assert CF.token_from_headers({"cf-access-jwt-assertion": "H", "cookie": "CF_Authorization=C"}) == "H"
    assert CF.token_from_headers({"cookie": "a=1; CF_Authorization=C; b=2"}) == "C"
    assert CF.token_from_headers({}) is None


def _raises(exc):
    def key_for_token(tok):
        raise exc
    return key_for_token


@pytest.mark.parametrize("exc, status", [
    (jwt.exceptions.PyJWKClientConnectionError("x"), 503), (ValueError("unknown kid"), 401),
])
def test_key_resolver_failure_status(exc, status):
    with pytest.raises(CF.AuthRejected) as caught:
        CF.CfAccessVerifier(CFG, key_for_token=_raises(exc)).verify(_tok())
    assert caught.value.status == status


def _svc_tok(cid="dev-a.access", aud="aud-1"):
    now = int(time.time())
    return jwt.encode({"common_name": cid, "sub": "", "aud": aud, "iat": now, "exp": now + 600,
                       "iss": "https://t.cloudflareaccess.com", "type": "app"}, KEY, algorithm="RS256")


def test_service_token_jwt_is_a_service_principal_only_when_allowed():
    v = CF.CfAccessVerifier(CFG, key_for_token=lambda tok: PUB, allow_service=lambda cid: cid == "dev-a.access")
    assert v.verify(_svc_tok()) == "service:dev-a.access"
    with pytest.raises(CF.AuthRejected) as exc:
        v.verify(_svc_tok("dev-z.access"))
    assert exc.value.status == 401


def test_without_allow_service_a_service_token_is_rejected_as_before():
    """The MCP listener's verifier passes no allow_service: unchanged behaviour."""
    with pytest.raises(CF.AuthRejected, match="email not allowlisted"):
        _verifier().verify(_svc_tok())


def test_an_email_claim_cannot_pose_as_a_service_principal():
    """`service:` is reserved: a person's token never resolves through library_tokens."""
    v = CF.CfAccessVerifier(CFG, key_for_token=lambda tok: PUB, allow=lambda e: True,
                            allow_service=lambda cid: True)
    with pytest.raises(CF.AuthRejected) as exc:
        v.verify(_tok(email="service:dev-a.access"))
    assert exc.value.status == 401
    assert v.verify(_tok()) == "alice@example.com"          # a person is still a person
