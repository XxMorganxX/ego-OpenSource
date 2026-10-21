import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from kb_core import llm_jobs as J
from kb_core.ingest import agent_queue_client as aqc
from kb_core.ingest.agent_queue_client import AgentQueueClient, JobWaiter
from kb_core.ingest.inference import InferenceClient, InferenceError, ProviderUnavailable


@pytest.fixture
def clean(pool):
    with pool.connection() as c:
        c.execute("TRUNCATE llm_jobs")
    return pool


def _agent(pool, *, reply=None, error=None, retry_after=None, delay=0.0):
    """A fake tenant agent: claims the next job and answers it. These tests use producer
    windows of seconds, so it claims regardless of the 15 s minimum a real claim keeps."""
    def run():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with pool.connection() as c:
                jobs = J.claim(c, max_jobs=1, worker="fake", min_remaining_s=0)
                if jobs:
                    time.sleep(delay)
                    j = jobs[0]
                    if error is None:
                        J.complete(c, j.id, j.claim_token, reply)
                    else:
                        J.fail(c, j.id, j.claim_token, error, retry_after=retry_after)
                    return
            time.sleep(0.05)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _failing_agent(pool, *, error, times):
    """A fake tenant agent that claims and plainly fails the same job `times` times.

    A plain failure below MAX_ATTEMPTS returns the job to pending, so it can re-claim it."""
    def run():
        failures = 0
        deadline = time.monotonic() + 5
        while failures < times and time.monotonic() < deadline:
            with pool.connection() as c:
                jobs = J.claim(c, max_jobs=1, worker="fake", min_remaining_s=0)
                if jobs:
                    j = jobs[0]
                    J.fail(c, j.id, j.claim_token, error)
                    failures += 1
                    continue
            time.sleep(0.05)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_the_job_row_carries_the_producers_deadline(clean):
    """Enqueue time + this client's own timeout, so the gateway can hand it to the agent."""
    client = AgentQueueClient(clean, task="t", timeout_s=0.3, poll_s=0.05)
    with pytest.raises(ProviderUnavailable):
        client.complete("S", "U")
    with clean.connection() as c:
        row = c.execute("SELECT EXTRACT(EPOCH FROM deadline - created_at) AS w "
                        "FROM llm_jobs").fetchone()
    assert abs(float(row["w"]) - 0.3) < 0.05


def test_satisfies_the_protocol(clean):
    assert isinstance(AgentQueueClient(clean, task="doc_kind"), InferenceClient)
    assert AgentQueueClient(clean, task="x").provider == "agent"


def test_complete_returns_the_agents_reply(clean):
    client = AgentQueueClient(clean, task="doc_kind", timeout_s=5, poll_s=0.1)
    _agent(clean, reply='{"kind":"meeting"}')
    assert client.complete("SYS", "USER", schema={"type": "object"}) == '{"kind":"meeting"}'
    with clean.connection() as c:
        row = c.execute("SELECT task, system_prompt, user_prompt, schema, status FROM llm_jobs").fetchone()
    assert (row["task"], row["system_prompt"], row["user_prompt"], row["schema"], row["status"]) == \
        ("doc_kind", "SYS", "USER", {"type": "object"}, "done")


def test_complete_returns_raw_text_and_leaves_parsing_to_caller(clean):
    """Garbage from the model is returned as-is; the pass's own parser decides."""
    client = AgentQueueClient(clean, task="t", timeout_s=5, poll_s=0.1)
    _agent(clean, reply="not json at all")
    assert client.complete("S", "U", schema={"type": "object"}) == "not json at all"


def test_timeout_expires_the_job_and_pauses_the_provider(clean):
    client = AgentQueueClient(clean, task="t", timeout_s=0.3, pause_minutes=15, poll_s=0.05)
    t0 = datetime.now(timezone.utc)
    with pytest.raises(ProviderUnavailable) as exc:
        client.complete("S", "U")
    assert exc.value.provider == "agent"
    assert timedelta(minutes=14) < exc.value.until - t0 <= timedelta(minutes=15, seconds=1)
    with clean.connection() as c:
        assert c.execute("SELECT status FROM llm_jobs").fetchone()["status"] == "expired"


def test_failed_with_retry_after_becomes_provider_unavailable(clean):
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    client = AgentQueueClient(clean, task="t", timeout_s=5, poll_s=0.1)
    _agent(clean, error="quota", retry_after=until)
    with pytest.raises(ProviderUnavailable) as exc:
        client.complete("S", "U")
    assert abs(exc.value.until - until) < timedelta(seconds=1)


def test_failed_without_retry_after_is_a_plain_inference_error(clean):
    client = AgentQueueClient(clean, task="t", timeout_s=5, poll_s=0.1)
    # one fake agent fails the job MAX_ATTEMPTS times; the last plain failure is terminal
    agent = _failing_agent(clean, error="boom", times=J.MAX_ATTEMPTS)
    with pytest.raises(InferenceError, match="boom") as exc:
        client.complete("S", "U")
    assert not isinstance(exc.value, ProviderUnavailable)
    agent.join(5)
    with clean.connection() as c:
        row = c.execute("SELECT status, attempts FROM llm_jobs").fetchone()
    assert (row["status"], row["attempts"]) == ("failed", J.MAX_ATTEMPTS)


def test_completion_racing_the_timeout_is_returned_not_discarded(clean, monkeypatch):
    """The agent finishes between the client's last read and its expire(): expire matches
    nothing, so the finished result must be returned rather than a pause raised."""
    real_expire = aqc.llm_jobs.expire

    def expire_after_a_late_completion(conn, job_id):
        with clean.connection() as other:
            # past its deadline by now: stands in for a claim made just before it
            [j] = J.claim(other, max_jobs=1, worker="late", min_remaining_s=-60)
            J.complete(other, j.id, j.claim_token, "late but done")
        real_expire(conn, job_id)

    monkeypatch.setattr(aqc.llm_jobs, "expire", expire_after_a_late_completion)
    client = AgentQueueClient(clean, task="t", timeout_s=0.3, poll_s=0.05)
    assert client.complete("S", "U") == "late but done"
    with clean.connection() as c:
        assert c.execute("SELECT status FROM llm_jobs").fetchone()["status"] == "done"


def test_listen_wakes_the_waiter_before_the_poll_interval(clean):
    client = AgentQueueClient(clean, task="t", timeout_s=5, poll_s=30)
    _agent(clean, reply="woken", delay=0.2)
    t0 = time.monotonic()
    assert client.complete("S", "U") == "woken"
    assert time.monotonic() - t0 < 2.0


def test_waiter_is_one_listener_per_dsn(clean):
    a = JobWaiter.for_dsn(clean.conninfo)
    assert JobWaiter.for_dsn(clean.conninfo) is a
    ev = a.register(123)
    assert isinstance(ev, threading.Event)
    a.unregister(123)
