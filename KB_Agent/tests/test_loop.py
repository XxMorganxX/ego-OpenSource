import logging
import threading
from datetime import datetime, timedelta, timezone

from kb_agent.client import GatewayError
from kb_agent.config import AgentConfig
from kb_agent.loop import AgentLoop
from kb_agent.state import State


class FakeGateway:
    def __init__(self, claims=()):
        self.claims = list(claims)          # each: a list of jobs, or an exception to raise
        self.notes, self.manifests, self.results = [], [], []

    def claim_jobs(self, max, wait):
        item = self.claims.pop(0) if self.claims else []
        if isinstance(item, BaseException):
            raise item
        return item

    def post_notes(self, events):
        self.notes.append(events); return {}

    def post_manifest(self, ids, generation, allow_empty=False):
        self.manifests.append((ids, generation)); return {}

    def post_result(self, job_id, token, result):
        self.results.append((job_id, result)); return {}

    def post_failure(self, job_id, token, error, retry_after):
        return {}


class FakeRunner:
    def __init__(self):
        self.paused_until, self.batches, self.auth_stale = None, [], False

    def run_batch(self, jobs):
        self.batches.append(jobs); return {"done": len(jobs)}


def _loop(tmp_path, gw, *, logged_in=True, runner=None):
    vault = tmp_path / "V"; vault.mkdir(exist_ok=True)
    cfg = AgentConfig(gateway="https://g", tenant="t", secret="s" * 64, vault=vault)
    loop = AgentLoop(cfg, State(tmp_path / "state.sqlite"), gw, runner=runner or FakeRunner(),
                     auth=lambda: {"logged_in": logged_in, "email": "a@x"})
    loop.waits = []
    loop._wait = loop.waits.append
    return loop


def test_jobs_once_claims_and_runs_a_batch(tmp_path):
    gw = FakeGateway(claims=[[{"id": 1}]])
    loop = _loop(tmp_path, gw)
    assert loop.jobs_once(wait=0) == {"done": 1} and loop.runner.batches == [[{"id": 1}]]


def test_a_logged_in_status_is_cached_for_five_minutes(tmp_path):
    gw = FakeGateway(claims=[[], [], []])
    loop = _loop(tmp_path, gw)
    checks = []
    loop._auth = lambda: checks.append(1) or {"logged_in": True, "email": "a@x"}
    loop.jobs_once(wait=0); loop.jobs_once(wait=0)
    assert len(checks) == 1                                   # one `claude auth status` spawn
    loop._auth_ok_at -= 301
    loop.jobs_once(wait=0)
    assert len(checks) == 2


def test_an_auth_failure_rechecks_the_login_at_once(tmp_path, caplog):
    loop = _loop(tmp_path, FakeGateway(claims=[[], []]))
    answers = [{"logged_in": True}, {"logged_in": False}]
    loop._auth = lambda: answers.pop(0)
    loop.jobs_once(wait=0)
    loop.runner.auth_stale = True                             # a job just failed with an auth marker
    loop.runner.paused_until = datetime.now(timezone.utc) + timedelta(minutes=15)
    with caplog.at_level(logging.WARNING, logger="kb-agent"):
        assert loop.jobs_once(wait=0) == {}
    assert answers == [] and loop.runner.auth_stale is False
    assert "not logged in" in caplog.text and loop.waits == [60]   # told now, not after the pause


def test_a_vanished_binary_mid_window_is_rechecked_and_named_next_iteration(tmp_path, caplog, monkeypatch):
    import tempfile

    from kb_agent.jobs import ClaudeCli, JobRunner
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))     # the claude cwd lands here, not in $TMPDIR

    def gone(args, **kw):
        raise FileNotFoundError(args[0])
    job = {"id": 1, "task": "t", "system_prompt": "S", "user_prompt": "U", "claim_token": "tok"}
    gw = FakeGateway(claims=[[job], []])
    failures = []
    gw.post_failure = lambda jid, tok, err, retry: failures.append((jid, err, retry)) or {}
    loop = _loop(tmp_path, gw)
    loop.runner = JobRunner(loop.cfg, gw, make_client=lambda m: ClaudeCli(m, binary="/abs/claude", run=gone))
    answers = [{"logged_in": True},
               {"logged_in": False, "email": None, "error": "not_found", "path": "/abs/claude"}]
    loop._auth = lambda: answers.pop(0)
    assert loop.jobs_once(wait=0) == {"failed": 1}
    assert failures[0][2] is not None                          # deferred, not a burned attempt
    with caplog.at_level(logging.WARNING, logger="kb-agent"):
        assert loop.jobs_once(wait=0) == {}                    # the cached "logged in" was dropped
    assert answers == [] and "claude CLI not found at /abs/claude; re-run kb-agent init" in caplog.text


def test_a_logged_out_status_is_not_cached(tmp_path):
    loop = _loop(tmp_path, FakeGateway(), logged_in=False)
    checks = []
    loop._auth = lambda: checks.append(1) or {"logged_in": False}
    loop.jobs_once(wait=0); loop.jobs_once(wait=0)
    assert len(checks) == 2                                   # a fresh login is noticed within 60 s


def test_a_429_on_claim_honours_retry_after(tmp_path):
    loop = _loop(tmp_path, FakeGateway(claims=[GatewayError(429, "too many requests", retry_after=1.0)]))
    assert loop.jobs_once(wait=0) == {} and loop.waits == [1.0]


def test_other_claim_failures_back_off_ten_seconds(tmp_path):
    loop = _loop(tmp_path, FakeGateway(claims=[GatewayError(0, "URLError")]))
    assert loop.jobs_once(wait=0) == {} and loop.waits == [10]


def test_not_logged_in_claims_nothing(tmp_path):
    gw = FakeGateway(claims=[[{"id": 1}]])
    loop = _loop(tmp_path, gw, logged_in=False)
    assert loop.jobs_once(wait=0) == {} and loop.waits == [60] and gw.claims == [[{"id": 1}]]


def test_a_missing_claude_is_named_not_mistaken_for_logged_out(tmp_path, caplog):
    gw = FakeGateway(claims=[[{"id": 1}]])
    loop = _loop(tmp_path, gw)
    loop._auth = lambda: {"logged_in": False, "email": None, "error": "not_found", "path": "/abs/claude"}
    with caplog.at_level(logging.WARNING, logger="kb-agent"):
        assert loop.jobs_once(wait=0) == {}
    assert "claude CLI not found at /abs/claude; re-run kb-agent init" in caplog.text
    assert "auth login" not in caplog.text and gw.claims == [[{"id": 1}]]


def test_the_default_auth_check_uses_the_configured_claude_path(tmp_path, monkeypatch):
    import kb_agent.loop as L
    seen = []
    monkeypatch.setattr(L, "claude_auth_status", lambda *, binary: seen.append(binary) or {"logged_in": False})
    vault = tmp_path / "V"; vault.mkdir()
    cfg = AgentConfig(gateway="https://g", tenant="t", secret="s" * 64, vault=vault, claude_path="/abs/claude")
    loop = AgentLoop(cfg, State(tmp_path / "s.sqlite"), FakeGateway(), runner=FakeRunner())
    loop._wait = lambda s: None
    loop.jobs_once(wait=0)
    assert seen == ["/abs/claude"]


def test_a_paused_provider_claims_nothing_until_the_pause_ends(tmp_path):
    gw = FakeGateway(claims=[[{"id": 1}]])
    runner = FakeRunner(); runner.paused_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    loop = _loop(tmp_path, gw, runner=runner)
    assert loop.jobs_once(wait=0) == {} and gw.claims == [[{"id": 1}]]
    assert 590 < loop.waits[0] <= 600
    runner.paused_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert loop.jobs_once(wait=0) == {"done": 1} and runner.paused_until is None


def test_the_jobs_thread_survives_any_exception(tmp_path, caplog):
    gw = FakeGateway(claims=[RuntimeError("SECRET boom"), GatewayError(503, "unavailable"), [{"id": 2}]])
    loop = _loop(tmp_path, gw)

    def wait(seconds):
        loop.waits.append(seconds)
        if not gw.claims:
            loop._stop.set()
    loop._wait = wait
    real = loop.jobs_once

    def once(wait=20):
        out = real(wait=0)
        if out:
            loop._stop.set()
        return out
    loop.jobs_once = once
    with caplog.at_level(logging.WARNING, logger="kb-agent"):
        loop._jobs_forever()
    assert loop.runner.batches == [[{"id": 2}]]
    assert "RuntimeError" in caplog.text and "503" in caplog.text and "SECRET" not in caplog.text


def test_the_sync_loop_survives_any_exception(tmp_path, caplog):
    loop = _loop(tmp_path, FakeGateway())
    calls = []

    def sync_once(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("SECRET disk")
        if len(calls) == 2:
            raise GatewayError(429, "busy", retry_after=90.0)
        loop._stop.set()
        return {}
    loop.sync_once = sync_once
    with caplog.at_level(logging.WARNING, logger="kb-agent"):
        loop._sync_forever()
    assert len(calls) == 3 and loop.waits[:2] == [30, 90.0]           # scan_seconds, then Retry-After
    assert "OSError" in caplog.text and "SECRET" not in caplog.text


def test_sync_once_pushes_and_manifests_when_due(tmp_path):
    gw = FakeGateway()
    loop = _loop(tmp_path, gw)
    (loop.cfg.vault / "a.md").write_text("hello\n")
    out = loop.sync_once()
    assert out == {"notes": 1, "flushed": 0, "sent": 1, "manifest": True}
    assert len(gw.manifests) == 1
    assert loop.sync_once()["manifest"] is False                          # not due again for 600 s
    assert loop.sync_once(manifest=True)["manifest"] is True and len(gw.manifests) == 2


def test_a_withheld_manifest_stays_due(tmp_path, monkeypatch):
    import kb_agent.loop as L
    from kb_agent.scanner import Scan, ScanError
    gw = FakeGateway()
    loop = _loop(tmp_path, gw)
    monkeypatch.setattr(L, "scan_vault", lambda vault, exclude: Scan([], [ScanError("", True, "denied")]))
    assert loop.sync_once()["manifest"] is False and gw.manifests == []
    monkeypatch.undo()
    (loop.cfg.vault / "a.md").write_text("x")
    assert loop.sync_once()["manifest"] is True and len(gw.manifests) == 1


def test_run_forever_keeps_sqlite_on_the_calling_thread(tmp_path, caplog):
    """sqlite3 refuses a connection used from another thread; State is built by the caller."""
    gw = FakeGateway()
    loop = _loop(tmp_path, gw)
    (loop.cfg.vault / "a.md").write_text("hello\n")
    seen = []
    real = loop.sync_once

    def sync_once(**kw):
        seen.append(threading.current_thread())
        out = real(**kw)
        loop._stop.set()
        return out
    loop.sync_once = sync_once
    loop.jobs_once = lambda wait=20: loop._stop.wait(5) and {}
    with caplog.at_level(logging.WARNING, logger="kb-agent"):
        loop.run_forever()
    assert seen == [threading.current_thread()] and len(gw.notes) == 1 and "failed" not in caplog.text


def test_an_empty_claim_cannot_spin_the_jobs_thread(tmp_path):
    """A gateway that answers an empty claim at once must not turn the loop into a busy spin."""
    loop = _loop(tmp_path, FakeGateway())
    runs = []

    def once(wait=20):
        runs.append(1)
        if len(runs) == 3:
            loop._stop.set()
        return {}
    loop.jobs_once = once
    loop._jobs_forever()
    assert len(runs) == 3 and len(loop.waits) == 3 and all(0.9 < w <= 1.0 for w in loop.waits)
