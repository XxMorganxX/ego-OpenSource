import json
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from kb_core import llm_jobs as J
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture
def conn(pool):
    with pool.connection() as c:
        c.execute("TRUNCATE llm_jobs")
        c.execute("UPDATE agent_state SET last_seen_at = NULL, agent_version = NULL, hostname = NULL")
    with pool.connection() as c:
        yield c


def test_enqueue_claim_complete_round_trip(conn):
    jid = J.enqueue(conn, task="doc_kind", system_prompt="S", user_prompt="U",
                    schema={"type": "object"}, model_hint="haiku")
    assert J.get_outcome(conn, jid).status == "pending"
    [job] = J.claim(conn, max_jobs=4, worker="alice")
    assert (job.id, job.task, job.system_prompt, job.user_prompt, job.schema, job.model_hint) == \
        (jid, "doc_kind", "S", "U", {"type": "object"}, "haiku")
    assert job.attempts == 1 and job.claim_token
    assert J.claim(conn, max_jobs=4, worker="alice") == []            # nothing left pending
    assert J.complete(conn, jid, job.claim_token, '{"kind": "meeting"}') is True
    out = J.get_outcome(conn, jid)
    assert out.status == "done" and out.result == '{"kind": "meeting"}'
    assert J.complete(conn, jid, job.claim_token, "again") is False     # no longer claimed


def test_wrong_claim_token_is_refused(conn):
    jid = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")
    [job] = J.claim(conn, max_jobs=1, worker="a")
    assert J.complete(conn, jid, "not-the-token", "x") is False
    assert J.get_outcome(conn, jid).status == "claimed"


def test_fail_without_retry_requeues_until_max_attempts(conn):
    jid = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")
    for attempt in range(1, J.MAX_ATTEMPTS + 1):
        [job] = J.claim(conn, max_jobs=1, worker="a")
        assert job.attempts == attempt
        assert J.fail(conn, jid, job.claim_token, f"boom {attempt}") is True
        expected = "failed" if attempt == J.MAX_ATTEMPTS else "pending"
        assert J.get_outcome(conn, jid).status == expected
    assert J.get_outcome(conn, jid).error == f"boom {J.MAX_ATTEMPTS}"


def test_fail_with_retry_after_is_terminal_and_carries_the_time(conn):
    jid = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")
    [job] = J.claim(conn, max_jobs=1, worker="a")
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    assert J.fail(conn, jid, job.claim_token, "quota", retry_after=until) is True
    out = J.get_outcome(conn, jid)
    assert out.status == "failed" and abs(out.retry_after - until) < timedelta(seconds=1)


def test_expire_only_touches_open_jobs(conn):
    jid = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")
    J.expire(conn, jid)
    assert J.get_outcome(conn, jid).status == "expired"
    assert J.claim(conn, max_jobs=1, worker="a") == []                 # expired is not claimable
    J.expire(conn, jid)                                                 # idempotent
    assert J.get_outcome(conn, jid).status == "expired"


def test_reclaim_stale_returns_claims_to_pending_or_fails_them(conn):
    a = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")
    b = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")
    J.claim(conn, max_jobs=2, worker="a")
    conn.execute("UPDATE llm_jobs SET claimed_at = now() - interval '1 hour'")
    conn.execute("UPDATE llm_jobs SET attempts = %s WHERE id = %s", (J.MAX_ATTEMPTS, b))
    assert J.reclaim_stale(conn, older_than_s=1200) == 2
    assert J.get_outcome(conn, a).status == "pending"
    assert J.get_outcome(conn, b).status == "failed"


def test_claim_skips_locked_rows_across_connections(pool, conn):
    """Two agents for one tenant: a row claimed in an open transaction elsewhere is skipped."""
    a = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")
    b = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")
    conn.commit()
    other = psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row)
    try:
        other.execute("SELECT id FROM llm_jobs WHERE id = %s FOR UPDATE", (a,))   # held open
        got = J.claim(conn, max_jobs=5, worker="second-agent")
        assert [j.id for j in got] == [b]
    finally:
        other.rollback(); other.close()


def test_status_changes_notify_kb_llm_jobs(pool, conn):
    seen: list[dict] = []
    ready = threading.Event()

    def listener():
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as lc:
            lc.execute("LISTEN kb_llm_jobs")
            ready.set()
            # A suspended notifies() generator still holds the connection lock, so the
            # `with` exit (close) would deadlock and leave this thread alive forever —
            # closing() releases it on break and on any exception inside the loop.
            with closing(lc.notifies(timeout=5.0)) as gen:
                for n in gen:
                    seen.append(json.loads(n.payload))
                    if seen[-1]["status"] == "done":
                        break
    t = threading.Thread(target=listener); t.start(); ready.wait(2)
    jid = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U"); conn.commit()
    [job] = J.claim(conn, max_jobs=1, worker="a"); conn.commit()
    J.complete(conn, jid, job.claim_token, "ok"); conn.commit()
    t.join(6)
    assert [s["status"] for s in seen] == ["pending", "claimed", "done"] and all(s["id"] == jid for s in seen)


def test_enqueue_records_the_producers_deadline_and_claim_reports_it(conn):
    """The producer's window (enqueue + its timeout) rides on the row and the claim, so
    the agent can size its run to what is left instead of a flat 300 s."""
    jid = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U", timeout_s=600)
    old = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")    # no deadline known
    got = {j.id: j for j in J.claim(conn, max_jobs=5, worker="a")}
    assert 590 < got[jid].remaining_s <= 600
    assert got[jid].deadline is not None and got[jid].deadline.tzinfo is not None
    assert got[old].deadline is None and got[old].remaining_s is None


def test_claim_leaves_a_job_with_too_little_time_left(conn):
    """Under MIN_CLAIM_REMAINING_S of its window left, nobody can answer it in time: it stays
    pending for the producer to expire instead of burning the tenant's quota for a 409."""
    late = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U",
                     timeout_s=J.MIN_CLAIM_REMAINING_S - 5)
    ok = J.enqueue(conn, task="t", system_prompt="S", user_prompt="U",
                   timeout_s=J.MIN_CLAIM_REMAINING_S + 60)
    assert [j.id for j in J.claim(conn, max_jobs=5, worker="a")] == [ok]
    assert J.get_outcome(conn, late).status == "pending"


def test_stale_claims_return_inside_the_producers_window():
    """A crashed agent's claim must come back while the producer still waits (600 s): after
    the agent's own run timeout plus a margin, never 2x the producer timeout."""
    assert J.AGENT_RUN_TIMEOUT_S < J.STALE_CLAIM_S < 600 - J.MIN_CLAIM_REMAINING_S


def test_open_counts_and_agent_touch(conn):
    J.enqueue(conn, task="t", system_prompt="S", user_prompt="U")
    assert J.open_counts(conn) == {"pending": 1}
    assert J.agent_last_seen(conn) is None
    J.touch_agent(conn, version="0.1", install_id="3f2a9c0d1e4b4f5a8b6c7d8e9f0a1b2c")
    assert J.agent_last_seen(conn) is not None
