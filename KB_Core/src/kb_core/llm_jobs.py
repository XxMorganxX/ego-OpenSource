"""The `llm_jobs` queue: how a completion request reaches a tenant's own machine.

Pure SQL helpers over one connection; the producer side (AgentQueueClient) and the
consumer side (the gateway) both import this, so the state machine lives in one place:
pending -> claimed -> done | failed; claimed -> pending on a plain failure below
MAX_ATTEMPTS or a stale claim; pending|claimed -> expired when the producer stops waiting.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

MAX_ATTEMPTS = 3
CHANNEL = "kb_llm_jobs"
# Timing contract with kb-agent. The agent runs one job for at most
# AGENT_RUN_TIMEOUT_S (KB_Agent jobs.JOB_TIMEOUT_S; a KB_Agent test pins the two together),
# and less when the producer's window has less left. A job with under MIN_CLAIM_REMAINING_S
# left is not claimed: nobody can answer it before the producer expires it. A claim with no
# report after STALE_CLAIM_S (the agent's timeout + a margin) is a crashed agent's and goes
# back to the queue -- inside the producer's 600 s window, not after it.
AGENT_RUN_TIMEOUT_S = 300.0
MIN_CLAIM_REMAINING_S = 15.0
STALE_CLAIM_S = AGENT_RUN_TIMEOUT_S + 60.0


@dataclass(frozen=True)
class Job:
    id: int
    task: str
    system_prompt: str
    user_prompt: str
    schema: dict | None
    model_hint: str | None
    claim_token: str
    attempts: int
    deadline: datetime | None = None       # producer's enqueue + timeout; None = unknown
    remaining_s: float | None = None       # deadline - now() at claim time (server clock)


@dataclass(frozen=True)
class JobOutcome:
    status: str
    result: str | None
    error: str | None
    retry_after: datetime | None


def enqueue(conn, *, task: str, system_prompt: str, user_prompt: str,
            schema: dict | None = None, model_hint: str | None = None,
            timeout_s: float | None = None) -> int:
    """`timeout_s`: how long the producer will wait; stored as the row's deadline (DB clock)."""
    row = conn.execute(
        """INSERT INTO llm_jobs (task, system_prompt, user_prompt, schema, model_hint, deadline)
           VALUES (%(t)s, %(s)s, %(u)s, %(sc)s, %(m)s,
                   CASE WHEN %(to)s::float8 IS NULL THEN NULL
                        ELSE now() + make_interval(secs => %(to)s::float8) END)
           RETURNING id""",
        {"t": task, "s": system_prompt, "u": user_prompt,
         "sc": Jsonb(schema) if schema is not None else None, "m": model_hint,
         "to": float(timeout_s) if timeout_s is not None else None}).fetchone()
    return int(row["id"])


def get_outcome(conn, job_id: int) -> JobOutcome | None:
    row = conn.execute("SELECT status, result, error, retry_after FROM llm_jobs WHERE id = %s",
                       (job_id,)).fetchone()
    if row is None:
        return None
    return JobOutcome(row["status"], row["result"], row["error"], row["retry_after"])


def expire(conn, job_id: int) -> None:
    conn.execute("""UPDATE llm_jobs SET status = 'expired', finished_at = now()
                    WHERE id = %s AND status IN ('pending', 'claimed')""", (job_id,))


def claim(conn, *, max_jobs: int, worker: str,
          min_remaining_s: float = MIN_CLAIM_REMAINING_S) -> list[Job]:
    """Claim up to `max_jobs` pending jobs, oldest first, skipping any whose producer
    deadline is under `min_remaining_s` away (left pending; the producer expires it)."""
    rows = conn.execute(
        """UPDATE llm_jobs SET status = 'claimed', claimed_at = now(),
                               claim_token = gen_random_uuid()::text,
                               attempts = attempts + 1
           WHERE id IN (SELECT id FROM llm_jobs WHERE status = 'pending'
                          AND (deadline IS NULL
                               OR deadline > now() + make_interval(secs => %(min)s::float8))
                        ORDER BY created_at LIMIT %(n)s FOR UPDATE SKIP LOCKED)
           RETURNING id, task, system_prompt, user_prompt, schema, model_hint,
                     claim_token, attempts, deadline,
                     EXTRACT(EPOCH FROM deadline - now())::float8 AS remaining_s""",
        {"n": max_jobs, "min": float(min_remaining_s)}).fetchall()
    return [Job(r["id"], r["task"], r["system_prompt"], r["user_prompt"], r["schema"],
                r["model_hint"], r["claim_token"], r["attempts"], r["deadline"],
                r["remaining_s"]) for r in rows]


def complete(conn, job_id: int, claim_token: str, result: str) -> bool:
    cur = conn.execute(
        """UPDATE llm_jobs SET status = 'done', result = %s, finished_at = now()
           WHERE id = %s AND claim_token = %s AND status = 'claimed'""",
        (result, job_id, claim_token))
    return cur.rowcount == 1


def fail(conn, job_id: int, claim_token: str, error: str,
         retry_after: datetime | None = None) -> bool:
    """A provider-level failure (retry_after set) is terminal for this job: the producer
    pauses the provider and the NODE is retried later as a new job. A plain failure goes
    back to pending until MAX_ATTEMPTS."""
    cur = conn.execute(
        """UPDATE llm_jobs
           SET status = CASE WHEN %(ra)s::timestamptz IS NOT NULL OR attempts >= %(max)s
                             THEN 'failed' ELSE 'pending' END,
               error = %(err)s, retry_after = %(ra)s,
               claim_token = NULL, claimed_at = NULL,
               finished_at = CASE WHEN %(ra)s::timestamptz IS NOT NULL OR attempts >= %(max)s
                                  THEN now() ELSE NULL END
           WHERE id = %(id)s AND claim_token = %(tok)s AND status = 'claimed'""",
        {"ra": retry_after, "max": MAX_ATTEMPTS, "err": error, "id": job_id, "tok": claim_token})
    return cur.rowcount == 1


def reclaim_stale(conn, *, older_than_s: float) -> int:
    cur = conn.execute(
        """UPDATE llm_jobs
           SET status = CASE WHEN attempts >= %(max)s THEN 'failed' ELSE 'pending' END,
               error = CASE WHEN attempts >= %(max)s THEN 'claim expired' ELSE error END,
               finished_at = CASE WHEN attempts >= %(max)s THEN now() ELSE NULL END,
               claim_token = NULL, claimed_at = NULL
           WHERE status = 'claimed' AND claimed_at < now() - make_interval(secs => %(age)s)""",
        {"max": MAX_ATTEMPTS, "age": float(older_than_s)})
    return cur.rowcount


def open_counts(conn) -> dict[str, int]:
    rows = conn.execute("SELECT status, count(*) AS n FROM llm_jobs "
                        "WHERE status IN ('pending', 'claimed') GROUP BY status").fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def touch_agent(conn, *, version: str | None, install_id: str | None) -> None:
    # The `hostname` column predates the opaque id and keeps its name to avoid a migration:
    # it holds the agent's random per-install id, never the machine's hostname.
    conn.execute("""UPDATE agent_state SET last_seen_at = now(),
                        agent_version = COALESCE(%s, agent_version),
                        hostname = COALESCE(%s, hostname) WHERE id = 1""",
                 (version, install_id))


def agent_last_seen(conn) -> datetime | None:
    row = conn.execute("SELECT last_seen_at FROM agent_state WHERE id = 1").fetchone()
    return row["last_seen_at"] if row else None
