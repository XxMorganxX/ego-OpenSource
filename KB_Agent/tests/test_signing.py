import socket

from kb_agent.config import AgentConfig
from kb_agent.signing import sign, signed_headers

# Known-answer vectors for the request signature. The gateway's half
# (kb_core.gateway.auth) is pinned to the SAME list in KB_Core's
# tests/test_gateway_auth.py, which also checks the two copies stay equal.
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


def test_matches_the_gateways_vectors():
    assert len(VECTORS) >= 4
    for v in VECTORS:
        assert sign(v["secret"], v["ts"], v["nonce"], v["method"], v["path"], v["query"],
                    v["body"].encode()) == v["signature"], v


def test_signed_headers_carry_tenant_timestamp_nonce_signature_and_service_token(tmp_path):
    cfg = AgentConfig(gateway="https://g", tenant="alice", secret="a" * 64, cf_client_id="cid",
                      cf_client_secret="csec", vault=tmp_path)
    h = signed_headers(cfg, "POST", "/v1/notes", b"{}", ts=1700000000)
    assert h["X-KB-Tenant"] == "alice" and h["X-KB-Timestamp"] == "1700000000"
    assert h["X-KB-Signature"] == sign("a" * 64, "1700000000", h["X-KB-Nonce"], "POST", "/v1/notes", "", b"{}")
    assert h["CF-Access-Client-Id"] == "cid" and h["CF-Access-Client-Secret"] == "csec"
    assert h["X-KB-Agent-Version"]
    cfg2 = AgentConfig(gateway="https://g", tenant="alice", secret="a" * 64, vault=tmp_path)
    assert "CF-Access-Client-Id" not in signed_headers(cfg2, "GET", "/v1/status", b"")


def test_every_request_gets_a_fresh_nonce(tmp_path):
    cfg = AgentConfig(gateway="https://g", tenant="alice", secret="a" * 64, vault=tmp_path)
    nonces = {signed_headers(cfg, "GET", "/v1/status", b"", ts=1700000000)["X-KB-Nonce"] for _ in range(50)}
    assert len(nonces) == 50 and all(len(n) == 32 for n in nonces)


def test_the_query_is_signed(tmp_path):
    cfg = AgentConfig(gateway="https://g", tenant="alice", secret="a" * 64, vault=tmp_path)
    h = signed_headers(cfg, "GET", "/v1/jobs", b"", query="max=4&wait=25", ts=1700000000, nonce="n" * 32)
    assert h["X-KB-Signature"] == sign("a" * 64, "1700000000", "n" * 32, "GET", "/v1/jobs", "max=4&wait=25", b"")
    assert h["X-KB-Signature"] != signed_headers(cfg, "GET", "/v1/jobs", b"", ts=1700000000,
                                                 nonce="n" * 32)["X-KB-Signature"]


def test_the_agent_sends_an_opaque_install_id_not_the_hostname(tmp_path):
    """The agent does not send socket.gethostname() on any request. It
    sends the random per-install id from its config, and nothing machine-derived."""
    cfg = AgentConfig(gateway="https://g", tenant="alice", secret="a" * 64, vault=tmp_path)
    h = signed_headers(cfg, "GET", "/v1/status", b"")
    assert h["X-KB-Agent-Id"] == cfg.install_id and len(cfg.install_id) == 32
    assert "X-KB-Agent-Host" not in h
    assert socket.gethostname() not in "".join(h.values())
    other = AgentConfig(gateway="https://g", tenant="alice", secret="a" * 64, vault=tmp_path)
    assert other.install_id != cfg.install_id
    # The exact set of headers the agent sends; KB_Agent/README.md lists the same.
    assert set(h) == {"X-KB-Tenant", "X-KB-Timestamp", "X-KB-Nonce", "X-KB-Signature",
                      "X-KB-Agent-Version", "X-KB-Agent-Id", "Content-Type"}
