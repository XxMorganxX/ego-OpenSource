"""Provider `agent`: the completion runs on the TENANT's own machine.

`complete()` inserts an llm_jobs row and waits for the tenant's kb-agent to post the
reply. One `JobWaiter` per process holds the single LISTEN connection; waiters block on a
per-job Event with a poll fallback, so KB_ENRICH_CONCURRENCY callers never pin one pooled
connection each for the whole wait. Timeout => the job is expired and the caller gets
ProviderUnavailable, which the existing defer/pause machinery turns into a per-tenant
pause of provider `agent`.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg

from kb_core import llm_jobs
from kb_core.ingest.inference import InferenceError, ProviderUnavailable

_TERMINAL = ("done", "failed", "expired")


class JobWaiter:
    _instances: dict[str, "JobWaiter"] = {}
    _instances_lock = threading.Lock()

    @classmethod
    def for_dsn(cls, dsn: str) -> "JobWaiter":
        with cls._instances_lock:
            w = cls._instances.get(dsn)
            if w is None:
                w = cls._instances[dsn] = JobWaiter(dsn)
            return w

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def register(self, job_id: int) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._events[job_id] = ev
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="kb-job-waiter", daemon=True)
                self._thread.start()
        return ev

    def unregister(self, job_id: int) -> None:
        with self._lock:
            self._events.pop(job_id, None)

    def _run(self) -> None:
        while True:
            try:
                with psycopg.connect(self._dsn, autocommit=True) as conn:
                    conn.execute(f"LISTEN {llm_jobs.CHANNEL}")
                    # psycopg 3.3 keeps the connection lock while a paused notifies()
                    # generator exists, so the `with` exit (close) would deadlock on any
                    # error path. Bind it and close it explicitly before leaving the block.
                    gen = conn.notifies()
                    try:
                        for note in gen:
                            try:
                                payload = json.loads(note.payload)
                            except ValueError:
                                continue
                            if payload.get("status") in _TERMINAL:
                                with self._lock:
                                    ev = self._events.get(int(payload.get("id", -1)))
                                if ev is not None:
                                    ev.set()
                    finally:
                        gen.close()
            except Exception:
                time.sleep(1.0)                      # reconnect; waiters still poll


class AgentQueueClient:
    provider = "agent"

    def __init__(self, pool, *, task: str, timeout_s: float = 600.0,
                 pause_minutes: float = 15.0, model_hint: str | None = None,
                 waiter: JobWaiter | None = None, poll_s: float = 5.0) -> None:
        self._pool = pool
        self._task = task
        self._timeout = float(timeout_s)
        self._pause = timedelta(minutes=float(pause_minutes))
        self._model_hint = model_hint
        self._waiter = waiter or JobWaiter.for_dsn(pool.conninfo)
        self._poll = float(poll_s)

    @property
    def model(self) -> str:
        return self._model_hint or "tenant-claude"

    def complete(self, system_prompt: str, user_prompt: str, *,
                 schema: dict | None = None) -> str:
        with self._pool.connection() as conn:
            job_id = llm_jobs.enqueue(conn, task=self._task, system_prompt=system_prompt,
                                      user_prompt=user_prompt, schema=schema,
                                      model_hint=self._model_hint,
                                      timeout_s=self._timeout)
        ev = self._waiter.register(job_id)
        deadline = time.monotonic() + self._timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                ev.wait(timeout=max(0.0, min(self._poll, remaining)))
                ev.clear()
                with self._pool.connection() as conn:
                    out = llm_jobs.get_outcome(conn, job_id)
                result = self._settle(job_id, out)
                if result is not None:
                    return result
                if time.monotonic() >= deadline:
                    with self._pool.connection() as conn:
                        llm_jobs.expire(conn, job_id)
                        out = llm_jobs.get_outcome(conn, job_id)
                    # The agent may have finished between the last read and expire()
                    # (which only touches open jobs): honour that outcome, don't discard it.
                    result = self._settle(job_id, out)
                    if result is not None:
                        return result
                    raise ProviderUnavailable(
                        f"no agent answered job {job_id} within {self._timeout:g}s",
                        provider=self.provider,
                        until=datetime.now(timezone.utc) + self._pause)
        finally:
            self._waiter.unregister(job_id)

    def _settle(self, job_id: int, out: llm_jobs.JobOutcome | None) -> str | None:
        """`done` -> the reply text; `failed` -> raise; anything else -> None (still open)."""
        if out is not None and out.status == "done":
            return out.result or ""
        if out is not None and out.status == "failed":
            if out.retry_after is not None:
                raise ProviderUnavailable(
                    f"agent reported provider outage for job {job_id}: {out.error}",
                    provider=self.provider, until=out.retry_after)
            raise InferenceError(f"agent failed job {job_id}: {out.error}")
        return None
