import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from kb_agent.client import GatewayError
from kb_agent.config import AgentConfig
from kb_agent.jobs import JOB_TIMEOUT_S, ClaudeCli, JobError, JobRunner, ProviderPaused, claude_auth_status

SECRET_SYS = "SYS-SECRET-instructions"
SECRET_USER = "USER-SECRET-note-text"


class FakeGateway:
    def __init__(self, fail_result=()):
        self.results, self.failures = [], []
        self._fail_result = list(fail_result)

    def post_result(self, job_id, token, result):
        if self._fail_result:
            raise self._fail_result.pop(0)
        self.results.append((job_id, token, result)); return {}

    def post_failure(self, job_id, token, error, retry_after):
        self.failures.append((job_id, token, error, retry_after)); return {}


class FakeLLM:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def complete(self, system_prompt, user_prompt, *, schema=None):
        self.calls.append((system_prompt, user_prompt, schema))
        if self.exc:
            raise self.exc
        return self.reply


@pytest.fixture(autouse=True)
def _agent_home(tmp_path, monkeypatch):
    """Never let a test create the real support dir or the real per-user temp dir."""
    import tempfile
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path / "home"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))


def _cwd(tmp_path):
    return tmp_path / "tmp" / f"kb-agent-{os.getuid()}" / "claude-cwd"


def _cfg(tmp_path, **kw):
    return AgentConfig(gateway="https://g", tenant="t", secret="s" * 64, vault=tmp_path, **kw)


def _runner(tmp_path, llm, gw, **kw):
    return JobRunner(_cfg(tmp_path), gw, make_client=lambda model: llm, **kw)


JOB = {"id": 7, "task": "doc_kind", "system_prompt": "SYS", "user_prompt": "USER", "schema": {"type": "object"},
       "model_hint": "claude-haiku-4-5-20251001", "claim_token": "tok"}


def test_done_posts_the_reply_and_passes_the_schema(tmp_path):
    gw, llm = FakeGateway(), FakeLLM(reply='{"kind":"meeting"}')
    assert _runner(tmp_path, llm, gw).run_one(JOB) == "done"
    assert gw.results == [(7, "tok", '{"kind":"meeting"}')] and llm.calls[0] == ("SYS", "USER", {"type": "object"})


def test_garbage_output_is_still_posted_as_done(tmp_path):
    """The server-side parser decides; the agent never judges the text."""
    gw = FakeGateway()
    assert _runner(tmp_path, FakeLLM(reply="not json"), gw).run_one(JOB) == "done"
    assert gw.results[0][2] == "not json"


def test_quota_failure_posts_retry_after(tmp_path):
    until = datetime(2026, 10, 9, 18, tzinfo=timezone.utc)
    gw = FakeGateway()
    runner = _runner(tmp_path, FakeLLM(exc=ProviderPaused("limit", until=until)), gw)
    assert runner.run_one(JOB) == "failed"
    assert gw.failures == [(7, "tok", "limit", until.isoformat())]
    assert runner.paused_until == until                     # the loop stops claiming until then


def test_quota_failure_without_a_stated_reset_still_carries_a_retry_after(tmp_path):
    """Without retry_after the server re-queues the job and three quota hits fail the node; with
    it, the node is deferred. So an unparsed reset becomes now + 15 min."""
    gw = FakeGateway()
    runner = _runner(tmp_path, FakeLLM(exc=ProviderPaused("limit")), gw)
    assert runner.run_one(JOB) == "failed"
    retry = datetime.fromisoformat(gw.failures[0][3])
    assert timedelta(minutes=14) < retry - datetime.now(timezone.utc) <= timedelta(minutes=15)
    assert runner.paused_until == retry


def test_plain_failure_posts_error_without_retry_after(tmp_path):
    gw = FakeGateway()
    runner = _runner(tmp_path, FakeLLM(exc=JobError("claude exited 1: boom")), gw)
    assert runner.run_one(JOB) == "failed"
    assert gw.failures[0][2].startswith("claude exited 1") and gw.failures[0][3] is None
    assert runner.paused_until is None


def test_an_unexpected_exception_is_reported_by_class_name_only(tmp_path):
    gw = FakeGateway()
    assert _runner(tmp_path, FakeLLM(exc=ValueError(SECRET_USER)), gw).run_one(JOB) == "failed"
    assert gw.failures[0][2] == "ValueError"


def test_default_client_is_hardened_and_uses_the_model_hint(tmp_path, monkeypatch):
    import kb_agent.jobs as JM
    made = {}

    class _C:
        def __init__(self, model, **kw):
            made["model"], made["binary"] = model, kw.get("binary")

        def complete(self, *a, **k):
            return "ok"
    monkeypatch.setattr(JM, "ClaudeCli", _C)
    cfg = _cfg(tmp_path, claude_model="sonnet")
    JobRunner(cfg, FakeGateway()).run_one(JOB)
    assert made["model"] == "claude-haiku-4-5-20251001"
    JobRunner(cfg, FakeGateway()).run_one({**JOB, "model_hint": None})
    assert made["model"] == "sonnet"
    JobRunner(_cfg(tmp_path), FakeGateway()).run_one({**JOB, "model_hint": None})
    assert made["model"] == JM.DEFAULT_MODEL and made["binary"] == "claude"
    JobRunner(_cfg(tmp_path, claude_path="/abs/claude"), FakeGateway()).run_one(JOB)
    assert made["binary"] == "/abs/claude"                  # resolved at init: launchd's PATH may miss it


def test_a_model_hint_that_could_be_read_as_a_flag_is_ignored(tmp_path, monkeypatch):
    import kb_agent.jobs as JM
    made = []
    monkeypatch.setattr(JM, "ClaudeCli", lambda model, **kw: made.append(model) or FakeLLM(reply="ok"))
    for hint in ["--dangerously-skip-permissions", "-x", "a b", "x" * 200, ""]:
        JobRunner(_cfg(tmp_path), FakeGateway()).run_one({**JOB, "model_hint": hint})
    assert made == [JM.DEFAULT_MODEL] * 5


def test_a_stale_claim_is_reported_as_stale(tmp_path):
    gw = FakeGateway(fail_result=[GatewayError(409, "stale claim")])
    assert _runner(tmp_path, FakeLLM(reply="r"), gw).run_one(JOB) == "stale"


def test_a_throttled_result_post_honours_retry_after_and_is_retried(tmp_path):
    slept = []
    gw = FakeGateway(fail_result=[GatewayError(429, "busy", retry_after=1.0)])
    runner = _runner(tmp_path, FakeLLM(reply="r"), gw, sleep=slept.append)
    assert runner.run_one(JOB) == "done"
    assert slept == [1.0] and gw.results == [(7, "tok", "r")]


def test_a_long_retry_after_is_capped_so_a_worker_never_blocks_claiming(tmp_path):
    slept = []
    gw = FakeGateway(fail_result=[GatewayError(429, "busy", retry_after=3600.0)])
    assert _runner(tmp_path, FakeLLM(reply="r"), gw, sleep=slept.append).run_one(JOB) == "done"
    assert slept == [10.0]


def test_a_result_the_gateway_never_takes_is_failed_not_retried_forever(tmp_path):
    gw = FakeGateway(fail_result=[GatewayError(0, "URLError")] * 10)
    assert _runner(tmp_path, FakeLLM(reply="r"), gw, sleep=lambda s: None).run_one(JOB) == "failed"
    assert gw.results == [] and len(gw._fail_result) == 7          # three attempts, then give up


def test_run_batch_counts_outcomes(tmp_path):
    gw = FakeGateway()
    assert _runner(tmp_path, FakeLLM(reply="r"), gw).run_batch([JOB, {**JOB, "id": 8}]) == {"done": 2}


# ---- each run is sized to the producer's remaining window --------------------------------

class _Timed:
    """A ClaudeCli stand-in that records the timeout it was given."""
    def __init__(self):
        self.timeout, self.calls = JOB_TIMEOUT_S, []

    def complete(self, system_prompt, user_prompt, *, schema=None):
        self.calls.append(self.timeout)
        return "r"


@pytest.mark.parametrize("deadline_in_s, want", [
    (None, JOB_TIMEOUT_S),                  # no deadline (an old row): the flat timeout
    (1000.0, JOB_TIMEOUT_S),                # plenty left: the agent's own cap
    (100.0, 90.0),                          # remaining - 10 s to report back
])
def test_the_run_timeout_is_min_of_the_cap_and_the_remaining_window(tmp_path, deadline_in_s, want):
    llm = _Timed()
    runner = JobRunner(_cfg(tmp_path), FakeGateway(), make_client=lambda m: llm)
    assert runner.run_one({**JOB, "deadline_in_s": deadline_in_s}) == "done"
    assert llm.calls and abs(llm.calls[0] - want) < 0.5


def test_time_spent_queued_in_the_batch_counts_against_the_window(tmp_path):
    import time
    llm = _Timed()
    runner = JobRunner(_cfg(tmp_path), FakeGateway(), make_client=lambda m: llm)
    runner.run_one({**JOB, "deadline_in_s": 100.0}, received=time.monotonic() - 50)
    assert abs(llm.calls[0] - 40.0) < 0.5


def test_a_job_with_under_15_s_left_is_failed_fast_never_run(tmp_path, caplog):
    llm, gw = _Timed(), FakeGateway()
    runner = JobRunner(_cfg(tmp_path), gw, make_client=lambda m: llm)
    with caplog.at_level(logging.INFO, logger="kb-agent"):
        assert runner.run_one({**JOB, "deadline_in_s": 12.0}) == "skipped"
    assert llm.calls == []                                  # claude never ran: no quota spent
    [(jid, tok, error, retry_after)] = gw.failures
    assert (jid, tok, retry_after) == (7, "tok", None) and "deadline" in error
    assert "SECRET" not in caplog.text


def test_the_agent_timing_matches_the_servers_contract():
    """The gateway's stale reclaim and claim floor are sized from the agent's run timeout."""
    from kb_core import llm_jobs
    from kb_agent import jobs
    assert llm_jobs.AGENT_RUN_TIMEOUT_S == jobs.JOB_TIMEOUT_S
    assert llm_jobs.MIN_CLAIM_REMAINING_S == jobs.MIN_REMAINING_S
    assert llm_jobs.STALE_CLAIM_S > jobs.JOB_TIMEOUT_S + jobs.REPORT_MARGIN_S


def test_logs_carry_ids_and_sizes_never_prompt_or_reply_text(tmp_path, caplog):
    job = {**JOB, "system_prompt": SECRET_SYS, "user_prompt": SECRET_USER}
    with caplog.at_level(logging.DEBUG, logger="kb-agent"):
        _runner(tmp_path, FakeLLM(reply="REPLY-SECRET"), FakeGateway()).run_one(job)
        _runner(tmp_path, FakeLLM(exc=JobError("claude exited 1: x")), FakeGateway()).run_one(job)
    text = caplog.text
    assert "job 7" in text and "SECRET" not in text


# --- ClaudeCli: the subprocess boundary -------------------------------------------------------

class _Run:
    """Stands in for subprocess.run and records exactly what the child would have seen."""

    def __init__(self, returncode=0, stdout="", stderr="", exc=None):
        self.rc, self.stdout, self.stderr, self.exc, self.calls = returncode, stdout, stderr, exc, []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(args, self.rc, self.stdout, self.stderr)


def test_note_text_goes_on_stdin_only_and_the_system_template_only_in_system_prompt(tmp_path, monkeypatch, caplog):
    """Framing matches the owner's ClaudeCliInferenceClient: the server's prompt template (never
    note content) is `--system-prompt`, schema appended; the user prompt, which carries note
    text, is stdin and never argv (argv is visible in `ps`). Neither is ever logged."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    run = _Run(stdout="REPLY-SECRET\n")
    job = {**JOB, "system_prompt": SECRET_SYS, "user_prompt": SECRET_USER,
           "schema": {"type": "object", "title": "SCHEMA-MARK"}}
    gw = FakeGateway()
    runner = JobRunner(_cfg(tmp_path), gw, make_client=lambda m: ClaudeCli(m, run=run))
    with caplog.at_level(logging.DEBUG, logger="kb-agent"):
        assert runner.run_one(job) == "done"
    (args, kw), = run.calls
    assert kw["input"] == SECRET_USER                                   # stdin is exactly the user prompt
    system = args[args.index("--system-prompt") + 1]
    assert system.startswith(SECRET_SYS) and "SCHEMA-MARK" in system   # schema rides with the system text
    others = args[:args.index("--system-prompt") + 1] + args[args.index("--system-prompt") + 2:]
    assert not any("SECRET" in a for a in others)                       # nowhere else on argv
    assert SECRET_USER not in system and SECRET_SYS not in kw["input"]
    assert "SECRET" not in caplog.text and gw.results == [(7, "tok", "REPLY-SECRET")]
    assert args[:4] == ["claude", "-p", "--model", "claude-haiku-4-5-20251001"]
    assert args[-7:] == ["--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                         "--setting-sources", ""]
    assert kw["timeout"] == JOB_TIMEOUT_S and kw["capture_output"] is True and kw["encoding"] == "utf-8"
    assert "ANTHROPIC_API_KEY" not in kw["env"]                     # the subscription login, not a key
    assert kw["cwd"] == str(_cwd(tmp_path)) and not os.listdir(kw["cwd"])   # empty, fixed


def test_argv_and_stdin_are_identical_to_the_owner_client():
    """Same framing as kb_core's ClaudeCliInferenceClient(hardened=True), so tenant output matches
    owner output. (The test may import kb_core; the package may not.)"""
    from kb_core.ingest.claude_cli_client import ClaudeCliInferenceClient
    owner = []

    def owner_runner(args, stdin, timeout, cwd, env):
        owner.append((args, stdin))
        return subprocess.CompletedProcess(args, 0, "r", "")
    ClaudeCliInferenceClient(model="m", hardened=True, runner=owner_runner).complete(
        SECRET_SYS, SECRET_USER, schema={"type": "object"})
    run = _Run(stdout="r")
    ClaudeCli("m", run=run).complete(SECRET_SYS, SECRET_USER, schema={"type": "object"})
    assert (run.calls[0][0], run.calls[0][1]["input"]) == owner[0]


def test_the_cwd_is_one_fixed_private_empty_directory(tmp_path):
    """`claude -p` files transcripts under ~/.claude/projects/<cwd>, so a fresh tempdir per
    process would add a project directory on every restart."""
    run = _Run(stdout="r")
    ClaudeCli("m", run=run).complete("s", "u")
    ClaudeCli("m", run=run).complete("s", "u")
    first, second = (c[1]["cwd"] for c in run.calls)
    assert first == second == str(_cwd(tmp_path))
    assert oct(os.stat(first).st_mode & 0o777) == "0o700" and os.listdir(first) == []
    assert oct(os.stat(_cwd(tmp_path).parent).st_mode & 0o777) == "0o700"


def test_the_cwd_is_never_under_home(monkeypatch):
    """claude walks up from its cwd for CLAUDE.md files; under $HOME it would load ~/CLAUDE.md,
    which the owner client (a tempdir under /var/folders) never sees."""
    import tempfile
    from pathlib import Path

    from kb_agent.jobs import claude_cwd_path
    assert Path.home().resolve() not in claude_cwd_path().resolve().parents
    monkeypatch.setattr(tempfile, "tempdir", str(Path.home() / "tmp"))      # TMPDIR set inside $HOME
    assert claude_cwd_path() == Path("/private/tmp") / f"kb-agent-{os.getuid()}" / "claude-cwd"


def test_a_symlinked_or_foreign_cwd_is_refused(tmp_path):
    from kb_agent.jobs import _ensure_private_dir
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir()
    _cwd(tmp_path).parent.mkdir(mode=0o700)
    _cwd(tmp_path).symlink_to(elsewhere)
    run = _Run(stdout="r")
    gw = FakeGateway()
    runner = JobRunner(_cfg(tmp_path), gw, make_client=lambda m: ClaudeCli(m, run=run))
    assert runner.run_one(JOB) == "failed" and run.calls == []           # claude never ran
    assert "refusing claude cwd" in gw.failures[0][2] and gw.failures[0][3] is not None   # deferred
    mine = tmp_path / "mine"; mine.mkdir()
    with pytest.raises(ProviderPaused, match="refusing claude cwd"):
        _ensure_private_dir(mine, uid=os.getuid() + 1)                 # owned by someone else
    _ensure_private_dir(mine)                                          # but fine for its owner


def test_fenced_schema_replies_are_unwrapped_and_prose_is_not(tmp_path):
    fenced = '```json\n{"a": 1}\n```'
    assert ClaudeCli("m", run=_Run(stdout=fenced)).complete("s", "u", schema={}) == '{"a": 1}'
    assert ClaudeCli("m", run=_Run(stdout=fenced)).complete("s", "u") == fenced


def test_a_timeout_is_reported_to_the_gateway_as_failed_with_a_reason(tmp_path):
    """The server waits 600 s from ENQUEUE, so a late claim gets 300 s of claude, then is failed
    explicitly with a reason (never left for the 2x reclaim)."""
    run = _Run(exc=subprocess.TimeoutExpired(["claude"], 300.0))
    gw = FakeGateway()
    runner = JobRunner(_cfg(tmp_path), gw, make_client=lambda model: ClaudeCli(model, run=run))
    assert runner.run_one(JOB) == "failed"
    assert run.calls[0][1]["timeout"] == 300.0
    assert gw.failures[0][2] == "claude timed out after 300s" and gw.failures[0][3] is None


def test_a_vanished_binary_defers_the_job_instead_of_burning_its_attempts(tmp_path):
    """nvm removal, `npm uninstall -g`, an upgrade's symlink swap: with retry_after=None the
    server would retry the job to MAX_ATTEMPTS in seconds and fail it for good."""
    gw = FakeGateway()
    runner = JobRunner(_cfg(tmp_path), gw, make_client=lambda m: ClaudeCli(m, run=_Run(exc=FileNotFoundError())))
    assert runner.run_one(JOB) == "failed"
    assert gw.failures[0][2] == "claude CLI not found at claude; re-run kb-agent init"
    assert gw.failures[0][3] is not None and runner.paused_until is not None
    assert runner.auth_stale is True                          # the loop re-checks (and names it) at once


def test_a_crash_logs_only_the_first_stderr_line_truncated(tmp_path, caplog):
    stderr = "Error: " + "x" * 500 + "\nTraceback line with " + SECRET_USER + "\n"
    gw = FakeGateway()
    runner = JobRunner(_cfg(tmp_path), gw, make_client=lambda m: ClaudeCli(m, run=_Run(1, "", stderr)))
    with caplog.at_level(logging.DEBUG, logger="kb-agent"):
        assert runner.run_one(JOB) == "failed"
    assert "SECRET" not in caplog.text and "exit 1" in caplog.text and "x" * 500 not in caplog.text
    assert "SECRET" not in gw.failures[0][2] and gw.failures[0][2].startswith("claude exited 1: Error: ")
    assert len(gw.failures[0][2]) <= 500


def test_a_quota_message_pauses_the_provider(tmp_path):
    out = "You've hit your session limit · resets 5:30am (Europe/Berlin)\n"
    gw = FakeGateway()
    runner = JobRunner(_cfg(tmp_path), gw, make_client=lambda m: ClaudeCli(m, run=_Run(1, out, "")))
    assert runner.run_one(JOB) == "failed"
    assert "session limit" in gw.failures[0][2] and gw.failures[0][3] is not None
    assert "You've" not in gw.failures[0][2]                 # our marker constant, not the stdout line
    assert runner.paused_until is not None


RESET_SAMPLES = ["You've hit your session limit · resets 5:30am (Europe/Berlin)",
                 "You've hit your session limit · resets 11pm (Europe/London)",
                 "You've hit your weekly limit · resets Oct 12 at 5am",
                 "You've hit your weekly limit · resets Jan 3 at 7:15pm",
                 "resets Feb 30 at 5am", "resets 5am (Not/AZone)", "usage limit reached"]


def test_reset_parsing_matches_the_owner_client():
    from kb_core.ingest.claude_cli_client import _parse_reset as owner_parse

    from kb_agent.jobs import _parse_reset
    for now in (datetime(2026, 10, 9, 12, tzinfo=timezone.utc), datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)):
        for sample in RESET_SAMPLES:
            assert _parse_reset(sample, now) == owner_parse(sample, now), sample


def test_a_stated_weekly_reset_becomes_the_retry_after(tmp_path):
    from kb_agent.jobs import _parse_reset
    err = "You've hit your weekly limit · resets Oct 12 at 5am\n"
    gw = FakeGateway()
    runner = JobRunner(_cfg(tmp_path), gw, make_client=lambda m: ClaudeCli(m, run=_Run(1, "", err)))
    assert runner.run_one(JOB) == "failed"
    expected = _parse_reset(err)
    assert expected is not None and gw.failures[0][3] == expected.isoformat()
    assert runner.paused_until == expected


def test_an_auth_failure_marks_the_login_for_a_recheck(tmp_path):
    runner = JobRunner(_cfg(tmp_path), FakeGateway(),
                       make_client=lambda m: ClaudeCli(m, run=_Run(1, "Failed to authenticate. API Error: 401\n", "")))
    assert runner.run_one(JOB) == "failed" and runner.auth_stale is True
    quota = JobRunner(_cfg(tmp_path), FakeGateway(),
                      make_client=lambda m: ClaudeCli(m, run=_Run(1, "You've hit your session limit\n", "")))
    assert quota.run_one(JOB) == "failed" and quota.auth_stale is False


def test_stdout_never_reaches_the_gateway_on_a_failure(tmp_path):
    """stdout is model output; it must never land in an error body or a server log."""
    gw = FakeGateway()
    runner = JobRunner(_cfg(tmp_path), gw, make_client=lambda m: ClaudeCli(m, run=_Run(1, "MODEL-SECRET says\n", "")))
    assert runner.run_one(JOB) == "failed"
    assert gw.failures[0][2] == "claude exited 1 (no stderr)"


def test_claudes_own_directory_leads_path_so_env_node_resolves(monkeypatch):
    """`#!/usr/bin/env node` (npm, nvm) needs node on PATH; nvm keeps node beside claude."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    nvm = "/Users/a/.nvm/versions/node/v20.11.0/bin"
    run = _Run(stdout="r")
    ClaudeCli("m", binary=f"{nvm}/claude", run=run).complete("s", "u")
    status = _Run(stdout='{"loggedIn": true}')
    claude_auth_status(run=status, binary=f"{nvm}/claude")
    for _args, kw in (run.calls[0], status.calls[0]):
        assert kw["env"]["PATH"] == f"{nvm}:/usr/bin:/bin"
    bare = _Run(stdout="r")
    ClaudeCli("m", run=bare).complete("s", "u")                   # a bare name changes nothing
    assert bare.calls[0][1]["env"]["PATH"] == "/usr/bin:/bin"


def test_auth_status_reads_the_cli_and_fails_closed():
    ok = _Run(stdout='{"loggedIn": true, "email": "a@x"}')
    assert claude_auth_status(run=ok) == {"logged_in": True, "email": "a@x"}
    assert ok.calls[0][0] == ["claude", "auth", "status"] and ok.calls[0][1]["timeout"] > 0
    assert claude_auth_status(run=_Run(stdout="not json")) == {"logged_in": False, "email": None}
    assert claude_auth_status(run=_Run(1, '{"loggedIn": true}')) == {"logged_in": False, "email": None}
    assert claude_auth_status(run=_Run(exc=FileNotFoundError()), binary="/abs/claude") == \
        {"logged_in": False, "email": None, "error": "not_found", "path": "/abs/claude"}
