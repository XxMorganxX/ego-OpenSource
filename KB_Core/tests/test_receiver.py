import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kb_core.ingest.inbox import Inbox
from kb_core.ingest.receiver import (
    MIN_SECRET_LEN, InsecureSecretError, build_receiver, require_secret, sign, signed_headers,
)

SECRET = "abcd" * 16    # 64 chars, 4 distinct: passes the startup rule; low entropy so
                         # gitleaks' generic-api-key rule doesn't read it as a credential


class _FakeInbox:
    """Records what would be staged; refusal tests never need a database."""

    def __init__(self):
        self.calls = []

    def append(self, source, *, external_id, event_type, payload):
        self.calls.append((source, external_id, event_type, payload))
        return len(self.calls)


def _app(inbox, **kw):
    return TestClient(build_receiver(inbox, secret=SECRET, **kw))


def _signed(body: dict, path="/ingest/webhook", *, secret=SECRET, ts=None):
    raw = json.dumps(body).encode()
    return raw, signed_headers(secret, path, raw, ts=ts)


# --- staging (real inbox) -------------------------------------------------------------

def test_healthz(kb, pool):
    assert _app(Inbox(pool)).get("/healthz").status_code == 200


def test_valid_signature_stages_event(kb, pool):
    raw, headers = _signed({"event_type": "upsert", "external_id": "a", "payload": {"x": 1}})
    r = _app(Inbox(pool)).post("/ingest/webhook", content=raw, headers=headers)
    assert r.status_code == 202 and r.json()["queued"] > 0
    events = Inbox(pool).claim_pending("webhook")
    assert len(events) == 1 and events[0].external_id == "a"
    assert kb.external_ids_for_source("webhook") == set()       # staged only, not ingested


def test_bad_signature_rejected_and_stages_nothing(kb, pool):
    raw = json.dumps({"external_id": "a"}).encode()
    r = _app(Inbox(pool)).post("/ingest/webhook", content=raw, headers={
        "X-KB-Timestamp": str(int(time.time())), "X-KB-Signature": "deadbeef",
        "Content-Type": "application/json"})
    assert r.status_code == 401
    assert Inbox(pool).claim_pending("webhook") == []


# --- request authentication -----------------------------------------------------------

def test_valid_request_is_accepted():
    inbox = _FakeInbox()
    raw, headers = _signed({"external_id": "a", "payload": {"external_id": "a"}})
    r = _app(inbox).post("/ingest/webhook", content=raw, headers=headers)
    assert r.status_code == 202 and inbox.calls[0][:3] == ("webhook", "a", "upsert")


def test_legacy_body_only_signature_is_refused():
    """The old scheme (HMAC of the raw body alone) carried no timestamp, so a captured
    request could be replayed forever. It is no longer accepted."""
    import hashlib
    import hmac
    inbox = _FakeInbox()
    raw = json.dumps({"external_id": "a"}).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = _app(inbox).post("/ingest/webhook", content=raw,
                         headers={"X-KB-Signature": sig, "Content-Type": "application/json"})
    assert r.status_code == 401 and inbox.calls == []


@pytest.mark.parametrize("mutate", [
    lambda h: h.pop("X-KB-Timestamp"),
    lambda h: h.update({"X-KB-Timestamp": "not-a-number"}),
    lambda h: h.pop("X-KB-Signature"),
    lambda h: h.update({"X-KB-Signature": "0" * 64}),
    lambda h: h.update({"X-KB-Signature": b"\xe9" * 64}),       # non-ASCII: refused, not 500
])
def test_malformed_auth_headers_are_refused(mutate):
    inbox = _FakeInbox()
    raw, headers = _signed({"external_id": "a"})
    mutate(headers)
    r = _app(inbox).post("/ingest/webhook", content=raw, headers=headers)
    assert r.status_code == 401 and inbox.calls == []


def test_signature_binds_the_source():
    """The source is part of the signed path: a request signed for one source cannot be
    re-pointed at another."""
    inbox = _FakeInbox()
    raw, headers = _signed({"external_id": "a"}, path="/ingest/webhook")
    r = _app(inbox).post("/ingest/obsidian", content=raw, headers=headers)
    assert r.status_code == 401 and inbox.calls == []


def test_signature_binds_the_body():
    inbox = _FakeInbox()
    raw, headers = _signed({"external_id": "a"})
    r = _app(inbox).post("/ingest/webhook", content=raw.replace(b'"a"', b'"b"'),
                         headers=headers)
    assert r.status_code == 401 and inbox.calls == []


@pytest.mark.parametrize("offset", [-301, 301, -10_000])
def test_timestamp_outside_the_window_is_refused(offset):
    inbox = _FakeInbox()
    raw, headers = _signed({"external_id": "a"}, ts=int(time.time()) + offset)
    r = _app(inbox).post("/ingest/webhook", content=raw, headers=headers)
    assert r.status_code == 401 and inbox.calls == []


def test_replayed_request_is_refused():
    inbox = _FakeInbox()
    client = _app(inbox)
    raw, headers = _signed({"external_id": "a"})
    assert client.post("/ingest/webhook", content=raw, headers=headers).status_code == 202
    assert client.post("/ingest/webhook", content=raw, headers=headers).status_code == 401
    assert len(inbox.calls) == 1


def test_a_rejected_request_does_not_burn_its_signature():
    """Only accepted requests enter the replay cache: a 400 for bad JSON must not make
    the sender's corrected retry look like a replay — and the cache must not be fillable
    by requests that fail auth."""
    inbox = _FakeInbox()
    client = _app(inbox)
    ts = int(time.time())
    bad = b"{not json"
    h = signed_headers(SECRET, "/ingest/webhook", bad, ts=ts)
    assert client.post("/ingest/webhook", content=bad, headers=h).status_code == 400
    raw, headers = _signed({"external_id": "a"}, ts=ts)
    assert client.post("/ingest/webhook", content=raw, headers=headers).status_code == 202



def test_concurrent_replay_is_refused_while_the_first_copy_is_staging():
    """The replay check and the reservation happen before the staging await: a second copy
    that arrives while the first is still inside inbox.append must not stage twice."""
    import asyncio
    import threading

    import httpx

    started, release = threading.Event(), threading.Event()

    class _SlowInbox(_FakeInbox):
        def append(self, *a, **kw):
            started.set()
            release.wait(5)
            return super().append(*a, **kw)

    inbox = _SlowInbox()
    app = build_receiver(inbox, secret=SECRET)
    raw, headers = _signed({"external_id": "a"})

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = asyncio.create_task(c.post("/ingest/webhook", content=raw, headers=headers))
            await asyncio.to_thread(started.wait, 5)
            second = await c.post("/ingest/webhook", content=raw, headers=headers)
            release.set()
            return (await first).status_code, second.status_code

    assert asyncio.run(run()) == (202, 401)
    assert len(inbox.calls) == 1


def test_a_failed_staging_releases_its_signature():
    """If the inbox write fails, the sender's retry of the same request is not a replay."""
    class _FlakyInbox(_FakeInbox):
        fail = True

        def append(self, *a, **kw):
            if self.fail:
                self.fail = False
                raise RuntimeError("db down")
            return super().append(*a, **kw)

    inbox = _FlakyInbox()
    client = TestClient(build_receiver(inbox, secret=SECRET), raise_server_exceptions=False)
    raw, headers = _signed({"external_id": "a"})
    assert client.post("/ingest/webhook", content=raw, headers=headers).status_code == 500
    assert client.post("/ingest/webhook", content=raw, headers=headers).status_code == 202
    assert len(inbox.calls) == 1


# --- body size ------------------------------------------------------------------------

def test_oversized_body_is_refused_with_413_before_auth():
    inbox = _FakeInbox()
    raw = json.dumps({"external_id": "a", "payload": {"content": "x" * 2048}}).encode()
    headers = signed_headers(SECRET, "/ingest/webhook", raw)
    r = _app(inbox, max_body=1024).post("/ingest/webhook", content=raw, headers=headers)
    assert r.status_code == 413 and inbox.calls == []


def test_oversized_chunked_body_without_length_is_refused():
    inbox = _FakeInbox()

    def chunks():
        for _ in range(8):
            yield b"x" * 512

    r = _app(inbox, max_body=1024).post(
        "/ingest/webhook", content=chunks(),
        headers={"X-KB-Timestamp": str(int(time.time())), "X-KB-Signature": "0" * 64})
    assert r.status_code == 413 and inbox.calls == []


def test_body_at_the_limit_is_accepted():
    inbox = _FakeInbox()
    raw = json.dumps({"external_id": "a"}).encode()
    headers = signed_headers(SECRET, "/ingest/webhook", raw)
    r = _app(inbox, max_body=len(raw)).post("/ingest/webhook", content=raw, headers=headers)
    assert r.status_code == 202


# --- no unauthenticated surface -------------------------------------------------------

@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/log"])
def test_no_unauthenticated_read_surface(path):
    assert _app(_FakeInbox()).get(path).status_code == 404


# --- startup secret rule --------------------------------------------------------------

@pytest.mark.parametrize("value", [
    None, "", "   ", "dev-secret", "DEV-SECRET", "change-me", "change-me-app",
    "change-me-" + "a" * 40, "changeme" + "a" * 40, "replace-me-" + "a" * 40,
    "<generate with openssl rand -hex 32>" + "a" * 10, "test-secret", "secret",
    "a" * (MIN_SECRET_LEN - 1),
])
def test_require_secret_refuses_empty_short_or_placeholder(value):
    with pytest.raises(InsecureSecretError, match="KB_INGEST_SECRET"):
        require_secret(value)


def test_require_secret_refuses_a_single_repeated_character():
    with pytest.raises(InsecureSecretError):
        require_secret("a" * 64)


def test_require_secret_accepts_a_generated_secret():
    assert require_secret(SECRET) == SECRET
    assert require_secret("0123456789abcdef" * 4) == "0123456789abcdef" * 4


def test_build_receiver_refuses_a_weak_secret():
    with pytest.raises(InsecureSecretError):
        build_receiver(_FakeInbox(), secret="dev-secret")


# --- the documented shell recipe signs exactly what the server verifies ---------------

@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not on PATH")
def test_documented_openssl_recipe_matches_the_server_signature():
    body = '{"event_type":"upsert","external_id":"hello-1","payload":{"external_id":"hello-1"}}'
    ts = "1700000000"
    script = r"""
BODY_SHA="$(printf '%s' "$BODY" | openssl dgst -sha256 | sed 's/^.* //')"
printf '%s\n%s\n%s\n%s' "$TS" POST /ingest/webhook "$BODY_SHA" \
  | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //'
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True,
                         env={"BODY": body, "TS": ts, "SECRET": SECRET,
                              "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    assert out.stdout.strip() == sign(SECRET, ts, "POST", "/ingest/webhook", body.encode())

# --- shipped config: no usable credential in the example env or compose file ----------
# A value printed in a public repo is not a secret: copying .env.example as-is must leave
# every password and the ingest HMAC key empty (so the services refuse to start), and
# docker-compose.yml must not fall back to a default superuser or app password.

CORE = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = (CORE / ".env.example").read_text()
COMPOSE = (CORE / "docker-compose.yml").read_text()
ROLES = (CORE / "db" / "init" / "02-roles.sh").read_text()

SECRET_KEYS = ("POSTGRES_PASSWORD", "KB_APP_PASSWORD", "KB_INGEST_SECRET")


def _assignments(key: str) -> list[str]:
    """Values of uncommented KEY=value lines."""
    return re.findall(rf"^\s*(?:export\s+)?{key}=(.*)$", ENV_EXAMPLE, re.M)


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_env_example_ships_no_value_for(key):
    values = _assignments(key)
    assert values, f"{key} should be listed (blank) so setup tooling can fill it in"
    assert all(v.split("#")[0].strip() == "" for v in values), f"{key} has a value"


def test_env_example_says_how_to_generate_the_ingest_secret():
    block = ENV_EXAMPLE[ENV_EXAMPLE.index("KB_INGEST_SECRET") - 600:
                        ENV_EXAMPLE.index("KB_INGEST_SECRET=") + 40]
    assert "openssl rand -hex 32" in block
    assert "dev-secret" not in ENV_EXAMPLE      # the stale "falls back to" text is gone


def test_ingest_daemon_refuses_the_example_value_as_shipped():
    [value] = _assignments("KB_INGEST_SECRET")
    with pytest.raises(InsecureSecretError):
        require_secret(value.split("#")[0].strip())


@pytest.mark.parametrize("key", ["POSTGRES_PASSWORD", "KB_APP_PASSWORD"])
def test_compose_requires_the_password_with_no_fallback(key):
    m = re.search(rf"^\s*{key}:\s*(.+)$", COMPOSE, re.M)
    assert m, f"{key} missing from docker-compose.yml"
    value = m.group(1).strip()
    assert value.startswith(f"${{{key}:?"), f"{key} must be required: {value}"
    assert ":-" not in value


def test_role_init_has_no_default_app_password():
    assert "change-me-app" not in ROLES
    assert '"${KB_APP_PASSWORD:?' in ROLES
