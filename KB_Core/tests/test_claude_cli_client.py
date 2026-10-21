import pytest

from kb_core.ingest.claude_cli_client import DEFAULT_CLI_MODEL, ClaudeCliInferenceClient
from kb_core.ingest.inference import InferenceClient, InferenceError, build_inference_client


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Stands in for subprocess.run — records the argv + stdin, returns a scripted process."""
    def __init__(self, proc: "_Proc | None" = None, raise_exc: Exception | None = None) -> None:
        self.proc = proc or _Proc(stdout="ok")
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def __call__(self, args, stdin, timeout, cwd, env):
        self.calls.append({"args": args, "stdin": stdin, "timeout": timeout, "cwd": cwd,
                           "env": env})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.proc


def test_claude_cli_client_satisfies_inference_protocol():
    assert isinstance(ClaudeCliInferenceClient(runner=FakeRunner()), InferenceClient)


def test_complete_builds_print_invocation_user_via_stdin_returns_stdout():
    runner = FakeRunner(_Proc(stdout="  the reply \n"))
    client = ClaudeCliInferenceClient(model="sonnet", runner=runner)
    out = client.complete("SYS", "USER")
    assert out == "the reply"                                  # stdout, stripped
    args = runner.calls[0]["args"]
    assert args[0:2] == ["claude", "-p"]                       # default binary + print mode
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--system-prompt") + 1] == "SYS"    # schema-free: system verbatim
    assert runner.calls[0]["stdin"] == "USER"                  # user prompt piped on stdin


def test_complete_appends_schema_shape_to_system():
    runner = FakeRunner(_Proc(stdout="{}"))
    client = ClaudeCliInferenceClient(runner=runner)
    client.complete("SYS", "USER", schema={"type": "object", "required": ["entities"]})
    args = runner.calls[0]["args"]
    system = args[args.index("--system-prompt") + 1]
    assert "SYS" in system and "entities" in system and "schema" in system.lower()


def test_complete_strips_api_key_auth_from_subprocess_env(monkeypatch):
    # claude.ai login (subscription) is what we want; a set ANTHROPIC_API_KEY/AUTH_TOKEN — even
    # the empty string — outranks it and makes `claude -p` fail. The client must scrub both.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-placeholder")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("HOME", "/Users/example")          # must be preserved (auth lives here)
    runner = FakeRunner(_Proc(stdout="ok"))
    ClaudeCliInferenceClient(runner=runner).complete("s", "u")
    env = runner.calls[0]["env"]
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env.get("HOME") == "/Users/example"            # rest of the environment is inherited


def test_nonzero_exit_raises_inference_error_with_stderr():
    runner = FakeRunner(_Proc(returncode=1, stderr="boom"))
    client = ClaudeCliInferenceClient(runner=runner)
    with pytest.raises(InferenceError, match="boom"):
        client.complete("s", "u")


def test_nonzero_exit_surfaces_stdout_where_claude_writes_errors():
    # `claude -p` writes its error to STDOUT and exits non-zero, leaving stderr empty
    # (e.g. a usage-limit hit mid-crawl, a 4xx api_error). The InferenceError must surface
    # that stdout text — otherwise every failure reads 'claude CLI exited 1:' with no cause.
    runner = FakeRunner(_Proc(returncode=1, stdout="Claude AI usage limit reached", stderr=""))
    client = ClaudeCliInferenceClient(runner=runner)
    with pytest.raises(InferenceError, match="usage limit reached"):
        client.complete("s", "u")


def test_missing_binary_raises_inference_error():
    runner = FakeRunner(raise_exc=FileNotFoundError("claude"))
    client = ClaudeCliInferenceClient(runner=runner)
    with pytest.raises(InferenceError):
        client.complete("s", "u")


def test_build_inference_client_selects_claude_cli_for_both_aliases():
    for provider in ("claude-cli", "claude-p"):
        client = build_inference_client({"KB_INFERENCE_PROVIDER": provider})
        assert isinstance(client, ClaudeCliInferenceClient)
        assert client.model == DEFAULT_CLI_MODEL == "claude-sonnet-5"


def test_build_inference_client_explicit_provider_and_model_override_environment():
    client = build_inference_client(
        {"KB_INFERENCE_PROVIDER": "local", "KB_CLAUDE_CLI_MODEL": "opus"},
        provider="claude-cli", model="claude-sonnet-5")
    assert isinstance(client, ClaudeCliInferenceClient)
    assert client.model == "claude-sonnet-5"


def test_build_inference_client_claude_cli_model_override():
    client = build_inference_client(
        {"KB_INFERENCE_PROVIDER": "claude-cli", "KB_CLAUDE_CLI_MODEL": "opus"})
    assert client.model == "opus"


def test_build_inference_client_claude_cli_blank_model_falls_back_to_default():
    # A launcher forwarding ${KB_CLAUDE_CLI_MODEL:-} sets the var to '' when the user didn't
    # override it; env.get(key, default) returns that '' (key present, not absent), so the model
    # must fall back to the default explicitly — else `claude -p --model ''` fails with
    # 400 'model: String should have at least 1 character'.
    client = build_inference_client(
        {"KB_INFERENCE_PROVIDER": "claude-cli", "KB_CLAUDE_CLI_MODEL": ""})
    assert client.model == DEFAULT_CLI_MODEL == "claude-sonnet-5"


def test_schema_reply_wrapped_in_markdown_fences_is_unwrapped():
    fenced = '```json\n{"ok": true}\n```'
    runner = FakeRunner(_Proc(stdout=fenced))
    client = ClaudeCliInferenceClient(runner=runner)
    out = client.complete("SYS", "USER", schema={"type": "object"})
    assert out == '{"ok": true}'                               # fence gone -> json.loads-able
    # Without a schema the reply is returned verbatim (prose is never mangled).
    runner2 = FakeRunner(_Proc(stdout=fenced))
    client2 = ClaudeCliInferenceClient(runner=runner2)
    assert client2.complete("SYS", "USER") == fenced


def test_unfenced_and_partially_fenced_replies_pass_through():
    runner = FakeRunner(_Proc(stdout='{"ok": true}'))
    client = ClaudeCliInferenceClient(runner=runner)
    assert client.complete("S", "U", schema={}) == '{"ok": true}'
    runner2 = FakeRunner(_Proc(stdout='```json\n{"truncated": true}'))
    client2 = ClaudeCliInferenceClient(runner=runner2)
    # No closing fence -> not fence-enclosed -> unchanged (parse fails loudly downstream).
    assert client2.complete("S", "U", schema={}) == '```json\n{"truncated": true}'


def test_declares_provider_identity():
    from kb_core.ingest.claude_cli_client import ClaudeCliInferenceClient
    c = ClaudeCliInferenceClient(model="claude-sonnet-5")
    assert (c.provider, c.model) == ("claude-cli", "claude-sonnet-5")


def test_repeated_client_construction_in_one_process_shares_one_scratch_dir(monkeypatch):
    """_ensure_cwd used to call tempfile.mkdtemp() once per INSTANCE, and nothing
    anywhere ever removed the directory, so kb-claude-cli-* dirs piled up on disk.
    That was tolerable when one process built one long-lived client; a consumer
    that builds a fresh ClaudeCliInferenceClient per call (as the since-deleted
    LLM hop judge did per search_graph call) leaks one directory per call. The fix
    (_process_scratch_cwd) shares ONE directory across every client built in this
    process: N clients constructed and used -> one mkdtemp() call, not N."""
    import os
    import shutil

    import kb_core.ingest.claude_cli_client as ccc

    # raising=False: pre-fix code has no _shared_cwd attribute at all -- this must
    # still exercise the N-clients-in-one-process behaviour on THAT code (and fail
    # on the real assertion below, not on a missing attribute) to be a meaningful
    # RED for the fix this test is pinned to.
    monkeypatch.setattr(ccc, "_shared_cwd", None, raising=False)
    seen_cwds = set()
    try:
        for _ in range(5):
            runner = FakeRunner(_Proc(stdout="ok"))
            client = ClaudeCliInferenceClient(runner=runner)
            client.complete("sys", "user")
            seen_cwds.add(runner.calls[0]["cwd"])
        assert len(seen_cwds) == 1, f"expected 1 shared dir, got {len(seen_cwds)}: {seen_cwds}"
        (only_dir,) = seen_cwds
        assert os.path.isdir(only_dir)
        assert os.path.basename(only_dir).startswith("kb-claude-cli-")
    finally:
        for d in seen_cwds:
            shutil.rmtree(d, ignore_errors=True)


# --- transient-outage classification (ProviderUnavailable) ------------------------

from datetime import datetime, timezone

from kb_core.ingest.claude_cli_client import _parse_reset
from kb_core.ingest.inference import ProviderUnavailable


def test_provider_unavailable_is_an_inference_error():
    err = ProviderUnavailable("m", provider="claude-cli")
    assert isinstance(err, InferenceError)
    assert err.provider == "claude-cli" and err.until is None


def test_parse_reset_session_grammar_next_occurrence_in_stated_zone():
    # 4:41am CEST == 02:41Z; "resets 5:30am" is later the same CEST day -> 03:30Z.
    now = datetime(2026, 8, 12, 2, 41, tzinfo=timezone.utc)
    got = _parse_reset(
        "You've hit your session limit · resets 5:30am (Europe/Berlin)", now=now)
    assert got == datetime(2026, 8, 12, 3, 30, tzinfo=timezone.utc)


def test_parse_reset_session_grammar_rolls_to_tomorrow_when_past():
    # 6:00am CEST: 5:30am already passed -> tomorrow 5:30am CEST.
    now = datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc)
    got = _parse_reset("resets 5:30am (Europe/Berlin)", now=now)
    assert got == datetime(2026, 8, 13, 3, 30, tzinfo=timezone.utc)


def test_parse_reset_weekly_grammar_machine_local():
    # The weekly-limit grammar ("resets Aug 1 at 5am") carries no zone: machine-local.
    now = datetime(2026, 7, 30, 16, 0, tzinfo=timezone.utc)
    got = _parse_reset("You've hit your weekly limit · resets Aug 1 at 5am", now=now)
    assert got is not None and got.tzinfo is not None
    assert (got.month, got.day, got.hour, got.minute) == (8, 1, 5, 0)
    assert got > now


def test_parse_reset_unparseable_returns_none():
    assert _parse_reset("Failed to authenticate: OAuth session expired") is None
    assert _parse_reset("resets 5:30am (Not/AZone)") is None


def test_session_limit_exit_raises_provider_unavailable_with_until():
    runner = FakeRunner(_Proc(returncode=1,
        stdout="You've hit your session limit · resets 5:30am (Europe/Berlin)"))
    with pytest.raises(ProviderUnavailable) as exc_info:
        ClaudeCliInferenceClient(runner=runner).complete("s", "u")
    err = exc_info.value
    assert err.provider == "claude-cli"
    assert err.until is not None and err.until.tzinfo is not None
    assert str(err).startswith("claude CLI exited 1:")     # grammar unchanged


def test_auth_failure_raises_provider_unavailable_without_until():
    runner = FakeRunner(_Proc(returncode=1,
        stdout="Failed to authenticate: OAuth session expired and could not be refreshed"))
    with pytest.raises(ProviderUnavailable) as exc_info:
        ClaudeCliInferenceClient(runner=runner).complete("s", "u")
    assert exc_info.value.until is None


def test_generic_failure_stays_plain_inference_error():
    runner = FakeRunner(_Proc(returncode=1, stdout="api_error: upstream 500"))
    with pytest.raises(InferenceError) as exc_info:
        ClaudeCliInferenceClient(runner=runner).complete("s", "u")
    assert not isinstance(exc_info.value, ProviderUnavailable)


def test_json_mode_session_limit_raises_provider_unavailable():
    import json as _json
    envelope = _json.dumps({"type": "result", "is_error": True,
        "result": "You've hit your session limit · resets 5:30am (Europe/Berlin)"})
    runner = FakeRunner(_Proc(returncode=0, stdout=envelope))
    client = ClaudeCliInferenceClient(runner=runner, output_format="json")
    with pytest.raises(ProviderUnavailable) as exc_info:
        client.complete("s", "u")
    assert exc_info.value.until is not None
