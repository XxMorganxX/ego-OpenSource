import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from kb_agent.client import GatewayClient, GatewayError
from kb_agent.config import AgentConfig
from kb_agent.signing import sign


class _Resp(io.BytesIO):
    def __init__(self, body: dict, status=200):
        super().__init__(json.dumps(body).encode()); self.status = status

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _client(tmp_path, opener):
    cfg = AgentConfig(gateway="https://g.example", tenant="t", secret="s" * 64, vault=tmp_path)
    return GatewayClient(cfg, opener=opener)


def test_requests_are_signed_and_json(tmp_path):
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["method"], seen["body"], seen["headers"] = req.full_url, req.get_method(), req.data, dict(req.header_items())
        return _Resp({"jobs": []})
    c = _client(tmp_path, opener)
    assert c.claim_jobs(max=3, wait=5) == []
    assert seen["url"] == "https://g.example/v1/jobs?max=3&wait=5" and seen["method"] == "GET"
    h = {k.lower(): v for k, v in seen["headers"].items()}           # urllib re-cases header names
    # The query string is part of the signature: rewriting ?max= or ?wait= breaks it.
    assert h["x-kb-signature"] == sign("s" * 64, h["x-kb-timestamp"], h["x-kb-nonce"], "GET", "/v1/jobs",
                                       "max=3&wait=5", b"")


def test_non_ascii_text_goes_over_the_wire_as_utf8_not_escapes(tmp_path):
    """`\\uXXXX` escaping would inflate non-Latin note text up to 6x against the 8 MiB cap."""
    seen = {}

    def opener(req, timeout):
        seen["body"] = req.data
        return _Resp({})
    _client(tmp_path, opener).post_notes([{"type": "upsert", "text": "café 日本"}])
    assert "café 日本".encode() in seen["body"] and b"\\u" not in seen["body"]
    assert json.loads(seen["body"])["events"][0]["text"] == "café 日本"


def test_a_429_carries_its_retry_after(tmp_path):
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "1"}, io.BytesIO(b"{}"))
    with pytest.raises(GatewayError) as exc:
        _client(tmp_path, opener).claim_jobs(max=1, wait=0)
    assert exc.value.status == 429 and exc.value.retry_after == 1.0

    def no_header(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"{}"))
    with pytest.raises(GatewayError) as exc:
        _client(tmp_path, no_header).status()
    assert exc.value.retry_after is None


def test_probe_time_reads_the_server_date_even_from_a_401(tmp_path):
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized",
                                     {"Date": "Fri, 09 Oct 2026 18:00:00 GMT"}, io.BytesIO(b"{}"))
    assert _client(tmp_path, opener).probe_time() == datetime(2026, 10, 9, 18, tzinfo=timezone.utc)

    def unreachable(req, timeout):
        raise urllib.error.URLError("down")
    assert _client(tmp_path, unreachable).probe_time() is None

    def no_date(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {"Date": "garbage"}, io.BytesIO(b"{}"))
    assert _client(tmp_path, no_date).probe_time() is None


def test_http_errors_become_gateway_error(tmp_path):
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b'{"detail":"unauthorized"}'))
    with pytest.raises(GatewayError) as exc:
        _client(tmp_path, opener).status()
    assert exc.value.status == 401
    assert exc.value.detail == "unauthorized" and str(exc.value) == "gateway 401: unauthorized"


def test_a_422_echoing_note_text_never_puts_the_text_in_the_error(tmp_path):
    body = json.dumps({"detail": [{"type": "string_too_long", "loc": ["body", "events", 0, "text"],
                                   "msg": "String too long", "input": "SECRET NOTE TEXT"}]}).encode()

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable Entity", {}, io.BytesIO(body))
    with pytest.raises(GatewayError) as exc:
        _client(tmp_path, opener).post_notes([{"type": "upsert", "text": "SECRET NOTE TEXT"}])
    assert exc.value.status == 422
    assert "SECRET" not in str(exc.value) and "SECRET" not in repr(exc.value.args)
    assert "SECRET" not in exc.value.detail and "String too long" in exc.value.detail


def test_a_non_json_body_is_a_gateway_error_without_the_body(tmp_path):
    def opener(req, timeout):
        return _RawResp(b"<html>Sign in to Cloudflare Access SECRET</html>")
    with pytest.raises(GatewayError) as exc:
        _client(tmp_path, opener).status()
    assert "SECRET" not in str(exc.value) and "html" not in str(exc.value).lower()


def test_a_non_json_error_body_falls_back_to_the_reason_phrase(tmp_path):
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"<html>SECRET</html>"))
    with pytest.raises(GatewayError) as exc:
        _client(tmp_path, opener).status()
    assert exc.value.status == 502 and exc.value.detail == "Bad Gateway"


def test_a_timeout_while_reading_is_a_gateway_error(tmp_path):
    class _Slow(_RawResp):
        def read(self, *a):
            raise TimeoutError("timed out")
    with pytest.raises(GatewayError) as exc:
        _client(tmp_path, lambda req, timeout: _Slow(b"")).status()
    assert exc.value.status == 0


class _RawResp(io.BytesIO):
    status = 200

    def __enter__(self): return self

    def __exit__(self, *a): return False
