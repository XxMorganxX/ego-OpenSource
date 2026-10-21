"""The agent's two loops: sync (scan + push + manifest) and jobs (long-poll + run).

Sync runs on the calling thread because `State`'s sqlite connection is bound to the thread
that opened it; jobs run on a daemon thread and never touch `State`. Neither loop dies on an
exception: it logs at WARNING (gateway status/detail, else the exception class only, since
some exceptions quote their input) and carries on after a backoff.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from kb_agent.client import GatewayClient, GatewayError
from kb_agent.config import AgentConfig
from kb_agent.jobs import JobRunner, claude_auth_status, not_found_message
from kb_agent.pusher import Pusher
from kb_agent.scanner import scan_vault
from kb_agent.state import State

log = logging.getLogger("kb-agent")

CLAIM_BACKOFF_S = 10
LOGGED_OUT_BACKOFF_S = 60
JOBS_ERROR_BACKOFF_S = 5
JOBS_MIN_ITERATION_S = 1.0
AUTH_CACHE_S = 300           # a logged-in answer is reused this long; a logged-out one never is


def _safe(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, GatewayError) else type(exc).__name__


class AgentLoop:
    def __init__(self, cfg: AgentConfig, state: State, gateway: GatewayClient | None = None, *,
                 runner: JobRunner | None = None, auth=None) -> None:
        self.cfg, self.state = cfg, state
        self.gateway = gateway or GatewayClient(cfg)
        self.pusher = Pusher(cfg, state, self.gateway)
        self.runner = runner or JobRunner(cfg, self.gateway)
        self._auth = auth or (lambda: claude_auth_status(binary=cfg.claude_path or "claude"))
        self._stop = threading.Event()
        self._last_manifest: float | None = None       # monotonic time of the last accepted one
        self._auth_ok_at: float | None = None           # monotonic time of the last logged-in answer

    def _wait(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def stop(self) -> None:
        self._stop.set()

    def sync_once(self, *, manifest: bool | None = None) -> dict:
        """One scan + push. `manifest`: None = post it when due, True = force, False = skip."""
        observed = scan_vault(self.cfg.vault, self.cfg.exclude)
        flushed = self.pusher.flush_pending()
        sent = self.pusher.push(self.pusher.diff(observed))
        if manifest is None:
            manifest = (self._last_manifest is None
                        or time.monotonic() - self._last_manifest >= self.cfg.manifest_seconds)
        manifested = False
        if manifest:
            try:
                manifested = self.pusher.manifest(observed) is not None      # None = withheld
            except GatewayError as exc:
                log.warning("manifest not accepted: %s", exc)
            if manifested:
                self._last_manifest = time.monotonic()
        summary = {"notes": len(observed), "flushed": flushed, "sent": sent, "manifest": manifested}
        log.log(logging.INFO if (flushed or sent or manifested) else logging.DEBUG,
                "sync: notes=%d flushed=%d sent=%d manifest=%s", len(observed), flushed, sent, manifested)
        return summary

    def _auth_status(self) -> dict:
        """`claude auth status` spawns node, so a logged-in answer is cached for AUTH_CACHE_S. An
        auth failure on a job drops the cache, so the next call asks again at once."""
        if getattr(self.runner, "auth_stale", False):
            self.runner.auth_stale = False
            self._auth_ok_at = None
        if self._auth_ok_at is not None and time.monotonic() - self._auth_ok_at < AUTH_CACHE_S:
            return {"logged_in": True}
        auth = self._auth()
        self._auth_ok_at = time.monotonic() if auth.get("logged_in") else None
        return auth

    def jobs_once(self, wait: int = 20) -> dict[str, int]:
        # The login first: after an auth failure the user hears "not logged in" now, not after
        # the pause below.
        auth = self._auth_status()
        if not auth.get("logged_in"):
            if auth.get("error") == "not_found":
                log.warning("%s; not claiming jobs", not_found_message(auth.get("path")))
            else:
                log.warning("claude is not logged in; not claiming jobs (run: claude auth login)")
            self._wait(LOGGED_OUT_BACKOFF_S)
            return {}
        paused = self.runner.paused_until
        if paused is not None:
            left = (paused - datetime.now(timezone.utc)).total_seconds()
            if left > 0:
                log.info("claude is paused (quota or auth); claiming again in %.0f min", left / 60)
                self._wait(left)
                return {}
            self.runner.paused_until = None
        try:
            jobs = self.gateway.claim_jobs(max=self.cfg.job_concurrency, wait=wait)
        except GatewayError as exc:
            log.warning("claim failed: %s", exc)
            self._wait(exc.retry_after if exc.retry_after is not None else CLAIM_BACKOFF_S)
            return {}
        if not jobs:
            return {}
        counts = self.runner.run_batch(jobs)
        log.info("jobs: %s", counts)                    # counts only, never prompt text
        return counts

    def _sync_forever(self) -> None:
        while not self._stop.is_set():
            delay: float = self.cfg.scan_seconds
            try:
                self.sync_once()
            except Exception as exc:
                log.warning("sync failed: %s", _safe(exc))
                if isinstance(exc, GatewayError) and exc.retry_after is not None:
                    delay = max(delay, exc.retry_after)
            self._wait(delay)

    def _jobs_forever(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.jobs_once()
            except Exception as exc:
                log.warning("jobs loop failed: %s", _safe(exc))
                self._wait(JOBS_ERROR_BACKOFF_S)
            # A gateway that answers an empty long-poll at once must not become a busy spin.
            elapsed = time.monotonic() - started
            if elapsed < JOBS_MIN_ITERATION_S:
                self._wait(JOBS_MIN_ITERATION_S - elapsed)

    def run_forever(self) -> None:
        jobs = threading.Thread(target=self._jobs_forever, name="jobs", daemon=True)
        jobs.start()
        try:
            self._sync_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
