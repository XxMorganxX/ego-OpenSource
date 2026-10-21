import ast
import hashlib
from pathlib import Path

import pytest

from kb_core.gateway import auth as A
from kb_core.tenants import Registry, Tenant
from tests.conftest import tenant_login

SECRET_A, SECRET_B = "a" * 64, "b" * 64

# Known-answer vectors for the gateway request signature. KB_Agent reimplements
# signing on purpose (the agent must not import the gateway), and its
# tests/test_signing.py carries the SAME list so both halves are pinned to one
# wire format; test_the_agent_pins_the_same_vectors keeps the two copies equal.
VECTORS = [
    {"secret": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "tenant": "alice",
     "ts": "1700000000", "nonce": "00112233445566778899aabbccddeeff", "method": "POST", "path": "/v1/notes", "query": "",
     "body": "{\"events\":[]}",
     "signature": "a4ec726bdad84e43f4755adabdd6c51722834f9ae7bc75259ffff4f29b742cde"},
    {"secret": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "tenant": "alice",
     "ts": "1700000000", "nonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0", "method": "POST", "path": "/v1/manifest", "query": "",
     "body": "{\"ids\":[\"x\"],\"generation\":1}",
     "signature": "c5721a25c27a7c758433b00d7d9e004fa6e932437d3b57dc330a28e98eabaa2a"},
    {"secret": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "tenant": "bob",
     "ts": "1700000999", "nonce": "b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0", "method": "GET", "path": "/v1/jobs", "query": "max=4&wait=25",
     "body": "",
     "signature": "5c9231434328f0be37db30870d7130a206322330bad1b20c4d58c74cfe9b4a9e"},
    {"secret": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "tenant": "carol",
     "ts": "1700001000", "nonce": "c0ffee00c0ffee00c0ffee00c0ffee00", "method": "POST", "path": "/v1/jobs/42", "query": "",
     "body": "{\"claim_token\":\"t\",\"status\":\"done\",\"result\":\"ok\"}",
     "signature": "37259660114a59821269abbee0071f45666beb828b03186f625228df8ffc3078"},
    {"secret": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "tenant": "dan",
     "ts": "1700002000", "nonce": "d00dd00dd00dd00dd00dd00dd00dd00d", "method": "GET", "path": "/v1/status", "query": "",
     "body": "",
     "signature": "9c4d1be4fd50a556aba7840cb4e9ef7d29e8a2ca7fef68db729cb091fc81f982"},
    {"secret": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "tenant": "erin",
     "ts": "1700003000", "nonce": "e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1", "method": "POST", "path": "/v1/notes", "query": "",
     "body": "{\"t\":\"café\"}",
     "signature": "3944f25c242407f9c09a4895e4a621ea49dc3c6937fe9e29c0161a4f3b3c5c9e"},
    {"secret": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "tenant": "bob",
     "ts": "1700000999", "nonce": "b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0", "method": "GET", "path": "/v1/jobs", "query": "max=1&wait=0",
     "body": "",
     "signature": "5235cc1094ea41ac2b09bfa1abf5fab45bd8b783d6bb11d45ce496084668b136"},
]


def _registry() -> Registry:
    owner = Tenant("owner", frozenset({"m@example.com"}), database="kb_mcp")
    a = Tenant("a", frozenset({"a@example.com"}), database="kb_t_a", agent_secret=SECRET_A, **tenant_login())
    b = Tenant("b", frozenset({"b@example.com"}), database="kb_t_b", agent_secret=SECRET_B, **tenant_login())
    off = Tenant("off", frozenset({"o@example.com"}), database="kb_t_off", agent_secret=SECRET_A, **tenant_login(), enabled=False)
    nokey = Tenant("nokey", frozenset({"n@example.com"}), database="kb_t_nokey")   # enabled, no agent yet
    return Registry(owner, {"a": a, "b": b, "off": off, "nokey": nokey}, env={})


NONCE = "00112233445566778899aabbccddeeff"


def test_sign_is_deterministic_and_covers_every_part():
    args = ("1700000000", NONCE, "POST", "/v1/notes", "", b'{"x":1}')
    base = A.sign(SECRET_A, *args)
    assert base == A.sign(SECRET_A, "1700000000", NONCE, "post", "/v1/notes", "", b'{"x":1}')  # method case-folded
    assert base != A.sign(SECRET_A, "1700000001", NONCE, "POST", "/v1/notes", "", b'{"x":1}')
    assert base != A.sign(SECRET_A, "1700000000", "f" * 32, "POST", "/v1/notes", "", b'{"x":1}')
    assert base != A.sign(SECRET_A, "1700000000", NONCE, "POST", "/v1/manifest", "", b'{"x":1}')
    assert base != A.sign(SECRET_A, "1700000000", NONCE, "POST", "/v1/notes", "a=1", b'{"x":1}')
    assert base != A.sign(SECRET_A, "1700000000", NONCE, "POST", "/v1/notes", "", b'{"x":2}')
    assert base != A.sign(SECRET_B, *args)


def test_canonical_string_is_the_documented_one():
    body = b'{"x":1}'
    assert A.canonical("1700000000", NONCE, "get", "/v1/jobs", "max=4&wait=25", body) == (
        b"1700000000\n" + NONCE.encode() + b"\nGET\n/v1/jobs\nmax=4&wait=25\n"
        + hashlib.sha256(body).hexdigest().encode())


def test_verify_accepts_a_correctly_signed_request():
    body = b'{"events": []}'
    h = A.signed_headers(SECRET_A, "a", "POST", "/v1/notes", body, ts=1700000000)
    t = A.verify(_registry(), h, "POST", "/v1/notes", body, now=1700000010)
    assert t.slug == "a"


@pytest.mark.parametrize("mutate, reason", [
    (lambda h: h.update({"X-KB-Signature": "0" * 64}), "bad signature"),
    (lambda h: h.update({"X-KB-Tenant": "b"}), "bad signature"),            # a's key, b's name
    (lambda h: h.update({"X-KB-Tenant": "nobody"}), "unknown or disabled"),
    (lambda h: h.update({"X-KB-Tenant": "owner"}), "unknown or disabled"),
    (lambda h: h.update({"X-KB-Tenant": "off"}), "unknown or disabled"),
    (lambda h: h.update({"X-KB-Tenant": "nokey"}), "unknown or disabled"),  # no agent_secret: never verifiable
    (lambda h: h.update({"X-KB-Timestamp": "soon"}), "bad timestamp"),
    (lambda h: h.pop("X-KB-Signature"), "bad signature"),
    (lambda h: h.pop("X-KB-Nonce"), "nonce"),
    (lambda h: h.update({"X-KB-Nonce": "short"}), "nonce"),
    (lambda h: h.update({"X-KB-Nonce": "x" * 129}), "nonce"),
    (lambda h: h.update({"X-KB-Nonce": "a b" * 8}), "nonce"),
    (lambda h: h.update({"X-KB-Nonce": "f" * 32}), "bad signature"),     # a nonce the HMAC did not cover
    (lambda h: h.update({"X-KB-Signature": "é" * 64}), "bad signature"),  # non-ASCII: refused, not TypeError
])
def test_verify_refuses(mutate, reason):
    body = b"{}"
    h = A.signed_headers(SECRET_A, "a", "POST", "/v1/manifest", body, ts=1700000000)
    mutate(h)
    with pytest.raises(A.GatewayAuthError, match=reason):
        A.verify(_registry(), h, "POST", "/v1/manifest", body, now=1700000000)


def test_verify_rejects_timestamp_skew():
    """A clock more than five minutes off is refused with a reason."""
    body = b"{}"
    h = A.signed_headers(SECRET_A, "a", "GET", "/v1/status", body, ts=1700000000)
    with pytest.raises(A.GatewayAuthError, match="skew"):
        A.verify(_registry(), h, "GET", "/v1/status", body, now=1700000000 + A.MAX_SKEW_S + 1)
    A.verify(_registry(), h, "GET", "/v1/status", body, now=1700000000 + A.MAX_SKEW_S)   # boundary ok


def test_headers_are_matched_case_insensitively():
    body = b"{}"
    h = {k.lower(): v for k, v in A.signed_headers(SECRET_A, "a", "GET", "/v1/status", body, ts=1700000000).items()}
    assert A.verify(_registry(), h, "GET", "/v1/status", body, now=1700000000).slug == "a"


def test_the_query_string_is_signed():
    """Unsigned, `?max=` and `?wait=` could be rewritten in flight without breaking the
    signature."""
    h = A.signed_headers(SECRET_A, "a", "GET", "/v1/jobs", b"", query="max=1&wait=0", ts=1700000000)
    assert A.verify(_registry(), h, "GET", "/v1/jobs", b"", query="max=1&wait=0", now=1700000000).slug == "a"
    for other in ("max=16&wait=0", "", "wait=0&max=1"):
        with pytest.raises(A.GatewayAuthError, match="bad signature"):
            A.verify(_registry(), h, "GET", "/v1/jobs", b"", query=other, now=1700000000)


def test_a_nonce_is_accepted_once_per_tenant_inside_the_window():
    replay: dict[str, A.ReplayCache] = {}
    h = A.signed_headers(SECRET_A, "a", "GET", "/v1/status", b"", ts=1700000000, nonce=NONCE)
    A.verify(_registry(), h, "GET", "/v1/status", b"", replay=replay, now=1700000000)
    with pytest.raises(A.GatewayAuthError, match="replayed nonce"):
        A.verify(_registry(), h, "GET", "/v1/status", b"", replay=replay, now=1700000100)
    # Same nonce, other tenant: its own namespace.
    hb = A.signed_headers(SECRET_B, "b", "GET", "/v1/status", b"", ts=1700000000, nonce=NONCE)
    A.verify(_registry(), hb, "GET", "/v1/status", b"", replay=replay, now=1700000000)
    # Identical requests in the same second differ only by their fresh nonce: both accepted.
    for _ in range(2):
        A.verify(_registry(), A.signed_headers(SECRET_A, "a", "GET", "/v1/status", b"", ts=1700000000),
                 "GET", "/v1/status", b"", replay=replay, now=1700000000)


def test_a_refused_signature_does_not_burn_the_nonce():
    replay: dict[str, A.ReplayCache] = {}
    h = A.signed_headers(SECRET_A, "a", "POST", "/v1/notes", b"{}", ts=1700000000, nonce=NONCE)
    with pytest.raises(A.GatewayAuthError, match="bad signature"):
        A.verify(_registry(), h, "POST", "/v1/notes", b"{tampered}", replay=replay, now=1700000000)
    assert len(replay.get("a", A.ReplayCache())) == 0
    A.verify(_registry(), h, "POST", "/v1/notes", b"{}", replay=replay, now=1700000000)


def test_replay_cache_expires_entries_with_the_window_and_stays_bounded():
    c = A.ReplayCache(max_entries=3)
    c.add("n1", 1700000000)
    assert c.seen("n1", 1700000000 + A.MAX_SKEW_S)
    assert not c.seen("n1", 1700000000 + A.MAX_SKEW_S + 1)              # skew check refuses it by then
    for i in range(5):
        c.add(f"m{i}", 1700001000)
    assert len(c) == 3 and not c.seen("m0", 1700001000) and c.seen("m4", 1700001000)


def test_precheck_needs_no_body():
    """The gateway runs this before reading the body: tenant, timestamp, nonce and signature
    shape are all settled from the headers alone."""
    h = A.signed_headers(SECRET_A, "a", "POST", "/v1/notes", b"x" * 100, ts=1700000000)
    claim = A.precheck(_registry(), h, now=1700000000)
    assert claim.tenant.slug == "a" and claim.nonce == h["X-KB-Nonce"]
    with pytest.raises(A.GatewayAuthError, match="skew"):
        A.precheck(_registry(), h, now=1700000000 + A.MAX_SKEW_S + 1)


def test_fixture_vectors_match_sign():
    assert len(VECTORS) >= 4
    for v in VECTORS:
        assert A.sign(v["secret"], v["ts"], v["nonce"], v["method"], v["path"], v["query"],
                      v["body"].encode()) == v["signature"], v


def test_the_agent_pins_the_same_vectors():
    """KB_Agent's test_signing.py holds its own copy of VECTORS (the agent package
    cannot import this one). Both copies must be identical, or the two signing
    halves could drift apart while each still passes against its own list."""
    agent_test = Path(__file__).resolve().parents[2] / "KB_Agent" / "tests" / "test_signing.py"
    tree = ast.parse(agent_test.read_text())
    copies = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "VECTORS" for t in node.targets)]
    assert copies == [VECTORS]
