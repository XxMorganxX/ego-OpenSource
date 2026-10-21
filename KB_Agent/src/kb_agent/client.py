"""HTTPS client for the gateway (stdlib urllib only)."""
from __future__ import annotations

import email.utils
import http.client
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from kb_agent.config import AgentConfig
from kb_agent.signing import signed_headers


class GatewayError(Exception):
    """`status` is the HTTP code (0 = the gateway was never reached or the reply was cut off).
    `detail` is safe to log: never raw body text, because a 422 echoes the request, note text included.
    `retry_after` is the server's Retry-After in seconds (a 429 carries one), else None."""

    def __init__(self, status: int, detail: str, retry_after: float | None = None) -> None:
        detail = detail[:200]
        super().__init__(f"gateway {status}: {detail}")
        self.status, self.detail, self.retry_after = status, detail, retry_after


MAX_RETRY_AFTER_S = 3600.0


def _retry_after(headers) -> float | None:
    """Retry-After as seconds from now: delta-seconds or an HTTP date; None if absent or garbled."""
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        when = _http_date(value)
        if when is None:
            return None
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _detail(raw: bytes, fallback: str) -> str:
    """The JSON `detail` (a string, or a validation list reduced to loc + msg, dropping `input`);
    else the fallback (the HTTP reason phrase)."""
    try:
        detail = json.loads(raw).get("detail")
    except (ValueError, AttributeError):
        return fallback
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, list):
        parts = [".".join(str(x) for x in d.get("loc", [])) + ": " + str(d.get("msg", ""))
                 for d in detail if isinstance(d, dict)]
        if parts:
            return "; ".join(parts)
    return fallback


class GatewayClient:
    def __init__(self, cfg: AgentConfig, *, opener=urllib.request.urlopen, timeout: float = 60.0) -> None:
        self._cfg, self._open, self._timeout = cfg, opener, timeout

    def _call(self, method: str, path: str, body: dict | None = None, *, query: str = "", timeout: float | None = None) -> dict:
        # ensure_ascii=False: non-ASCII text travels at its UTF-8 size, not as 6-byte \uXXXX escapes.
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        # The query is signed too, exactly as sent (without the "?").
        req = urllib.request.Request(self._cfg.gateway.rstrip("/") + path + (f"?{query}" if query else ""),
                                     data=raw if body is not None else None, method=method,
                                     headers=signed_headers(self._cfg, method, path, raw, query=query))
        try:
            with self._open(req, timeout=timeout or self._timeout) as resp:
                status = getattr(resp, "status", 200)
                data = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read() if exc.fp else b""
            except (OSError, http.client.HTTPException):
                err_body = b""
            raise GatewayError(exc.code, _detail(err_body, str(exc.reason)), _retry_after(exc.headers)) from None
        except urllib.error.URLError as exc:
            raise GatewayError(0, str(exc.reason)) from None
        except (OSError, http.client.HTTPException) as exc:   # timeouts, resets, truncated reads
            raise GatewayError(0, type(exc).__name__) from None
        try:
            reply = json.loads(data or b"{}")
        except ValueError:
            raise GatewayError(status, "response was not JSON") from None
        if not isinstance(reply, dict):
            raise GatewayError(status, "response was not a JSON object")
        return reply

    def post_notes(self, events: list[dict]) -> dict:
        return self._call("POST", "/v1/notes", {"events": events})

    def post_manifest(self, ids: list[str], generation: int, allow_empty: bool = False) -> dict:
        return self._call("POST", "/v1/manifest", {"ids": ids, "generation": generation, "allow_empty": allow_empty})

    def claim_jobs(self, max: int, wait: int) -> list[dict]:
        return self._call("GET", "/v1/jobs", query=f"max={int(max)}&wait={int(wait)}", timeout=wait + 15).get("jobs", [])

    def post_result(self, job_id: int, claim_token: str, result: str) -> dict:
        return self._call("POST", f"/v1/jobs/{job_id}", {"claim_token": claim_token, "status": "done", "result": result})

    def post_failure(self, job_id: int, claim_token: str, error: str, retry_after: str | None) -> dict:
        return self._call("POST", f"/v1/jobs/{job_id}", {"claim_token": claim_token, "status": "failed",
                                                         "error": error, "retry_after": retry_after})

    def status(self) -> dict:
        return self._call("GET", "/v1/status")

    def probe_time(self) -> datetime | None:
        """The server's clock, from the Date header of a status call whether or not it is
        accepted (a 401 still carries one); None when unknown. Lets `status` tell clock skew
        from a wrong secret."""
        req = urllib.request.Request(self._cfg.gateway.rstrip("/") + "/v1/status", method="GET",
                                     headers=signed_headers(self._cfg, "GET", "/v1/status", b""))
        try:
            with self._open(req, timeout=self._timeout) as resp:
                date = resp.headers.get("Date")
        except urllib.error.HTTPError as exc:
            date = exc.headers.get("Date") if exc.headers is not None else None
        except (OSError, http.client.HTTPException):       # URLError is an OSError
            return None
        return _http_date(date)
