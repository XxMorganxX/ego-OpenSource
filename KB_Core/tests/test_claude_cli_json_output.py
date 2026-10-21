"""`claude -p --output-format json`: the CLI's result envelope, and what we take from it.

Retrieval tracing wants cost.provider_tokens / cost.provider_cost_usd per query. Text mode prints
the model's reply and nothing else, so the envelope is the only place those numbers exist.
This mode is therefore additive and default-OFF: with the flag unset, argv and the returned
string are byte-identical to what enrichment has always produced, and the first test here
asserts that on the WHOLE argv rather than on a flag's absence.

Hermetic by construction: every test drives the injectable `runner` (or monkeypatches
`_run` for the factory-built clients, which take no runner). Nothing here spawns a real
`claude` subprocess and nothing here touches the network.

_Proc / FakeRunner are duplicated from test_claude_cli_client.py's helpers rather than imported.
That is this suite's convention (test_crawl_primitives.py's header records it) and it keeps
either file runnable on its own.
"""

import copy
import json
import subprocess

import pytest

# AnthropicInferenceClient comes from its own module, not from inference: inference.py
# imports it LAZILY inside build_inference_client, so it is never a module attribute
# there. Same split test_anthropic_client.py's imports already use.
from kb_core.ingest.anthropic_client import AnthropicInferenceClient
from kb_core.ingest.claude_cli_client import ClaudeCliInferenceClient
from kb_core.ingest.inference import (HTTPInferenceClient, InferenceError,
                                      build_inference_client)


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Stands in for subprocess.run — records the argv + stdin, returns a scripted process."""
    def __init__(self, proc: "_Proc | None" = None) -> None:
        self.proc = proc or _Proc(stdout="ok")
        self.calls: list[dict] = []

    def __call__(self, args, stdin, timeout, cwd, env):
        self.calls.append({"args": args, "stdin": stdin, "timeout": timeout, "cwd": cwd,
                           "env": env})
        return self.proc


# A REAL capture, abridged: `claude -p --output-format json` with every hardening flag
# applied. Exit 0. Note what `usage` actually is --
# NESTED dicts (server_tool_use, cache_creation) and a LIST (iterations). It is not a flat
# scalar map, which is why last_usage carries it verbatim and any consumer wanting one
# "provider tokens" number must SUM the four scalar counts itself.
_ENVELOPE = {
    "is_error": False, "duration_api_ms": 1386, "num_turns": 1,
    "stop_reason": "end_turn", "total_cost_usd": 0.000603,
    "usage": {"input_tokens": 181, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0, "output_tokens": 4,
              "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
              "service_tier": "standard",
              "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                 "ephemeral_5m_input_tokens": 0},
              "iterations": [{"input_tokens": 181, "output_tokens": 4, "type": "message"}]},
    "modelUsage": {}, "permission_denials": [], "subtype": "success",
    "api_error_status": None, "result": "ok", "type": "result",
}

# The claude CLI's session-limit message format, verbatim, with U+00B7 (·) as the
# separator. A downstream breaker parses it, so this string must survive into
# InferenceError unchanged in BOTH output modes.
QUOTA = "You've hit your session limit · resets 4:50pm (Europe/Berlin)"


def _envelope(**over) -> str:
    """One envelope as the CLI would print it. ensure_ascii=False because the CLI is Node:
    JSON.stringify emits the literal '·', so escaping it here would make the fixture
    weaker than reality."""
    env = copy.deepcopy(_ENVELOPE)
    env.update(over)
    return json.dumps(env, ensure_ascii=False)


def _capture_argv(client, monkeypatch, *, stdout) -> list:
    """argv for a client built by the FACTORY, which accepts no runner. Same seam the
    hardening tests use (monkeypatch the bound _run), so the two files agree on how a
    factory-built client is inspected."""
    seen = {}

    def fake_run(args, stdin):
        seen["args"] = args
        return _Proc(stdout=stdout)

    monkeypatch.setattr(client, "_run", fake_run)
    client.complete("sys", "user")
    return seen["args"]


def _timeout_message(client, monkeypatch) -> str:
    """Read a client's effective timeout out of its PUBLIC error text. The timeout never
    reaches argv — it is subprocess.run's own kwarg — so the InferenceError message is the
    only place it is observable without touching a private attribute."""
    def boom(args, stdin):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=0)

    monkeypatch.setattr(client, "_run", boom)
    with pytest.raises(InferenceError) as exc:
        client.complete("S", "U")
    return str(exc.value)


# ---------------------------------------------------------------------------
# the flag itself
# ---------------------------------------------------------------------------

def test_output_format_defaults_off_so_argv_and_reply_are_byte_identical():
    """The whole point of the default. This asserts the EXACT argv, not merely the absence
    of --output-format, because an exact assertion is the only shape that catches a future
    flag being appended unconditionally. `hardened=False` pins the unhardened base argv
    (the client hardens by default; test_claude_cli_hardening pins that)."""
    runner = FakeRunner(_Proc(stdout="  the reply \n"))
    out = ClaudeCliInferenceClient(model="sonnet", runner=runner,
                                   hardened=False).complete("SYS", "USER")
    assert out == "the reply"
    assert runner.calls[0]["args"] == [
        "claude", "-p", "--model", "sonnet", "--system-prompt", "SYS",
        "--exclude-dynamic-system-prompt-sections"]


def test_json_mode_appends_the_flag_and_preserves_the_base_argv():
    base_runner = FakeRunner(_Proc(stdout="ok"))
    ClaudeCliInferenceClient(model="sonnet", runner=base_runner,
                             hardened=False).complete("SYS", "USER")
    base = base_runner.calls[0]["args"]

    runner = FakeRunner(_Proc(stdout=_envelope()))
    ClaudeCliInferenceClient(model="sonnet", runner=runner, hardened=False,
                             output_format="json").complete("SYS", "USER")
    argv = runner.calls[0]["args"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv.count("--output-format") == 1
    assert argv[:len(base)] == base
    assert argv[len(base):] == ["--output-format", "json"]


def test_json_mode_and_hardening_coexist():
    """The seam with hardening. Assertions are INDEX-based on purpose: hardening appends four
    more flags to this same argv and must not be able to break them. If hardening is missing,
    this fails with TypeError on `hardened=` — which is the correct alarm, not a flake."""
    runner = FakeRunner(_Proc(stdout=_envelope()))
    ClaudeCliInferenceClient(runner=runner, output_format="json",
                             hardened=True).complete("S", "U")
    argv = runner.calls[0]["args"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv


def test_an_unsupported_output_format_is_rejected_at_construction():
    """Not style policing. `--output-format stream-json` needs --verbose and emits a
    newline-delimited event stream rather than one envelope; accepting it would hand the
    caller that stream verbatim as if it were the model's reply."""
    with pytest.raises(ValueError, match="stream-json"):
        ClaudeCliInferenceClient(output_format="stream-json")


# ---------------------------------------------------------------------------
# reading the envelope
# ---------------------------------------------------------------------------

def test_json_mode_returns_the_envelopes_result_not_the_raw_envelope():
    """The reply is `result`, and the ~700-byte envelope around it never reaches the
    caller. Asserted against a DISTINCTIVE result rather than the fixture's 2-char
    "ok": `assert "is_error" not in "ok"` cannot fail for any implementation, because
    a 2-character string cannot contain a 8-character key. The envelope's own keys
    are the discriminator, so they are named on both sides."""
    raw = _envelope(result="the model's actual answer")
    runner = FakeRunner(_Proc(stdout=raw))
    out = ClaudeCliInferenceClient(runner=runner, output_format="json").complete("S", "U")
    assert out == "the model's actual answer"
    assert "is_error" in raw              # ...it IS in what the CLI printed...
    assert "is_error" not in out          # ...and it is NOT in what we returned
    assert "total_cost_usd" not in out
    assert "usage" not in out


def test_last_usage_is_none_before_the_first_call():
    assert ClaudeCliInferenceClient(runner=FakeRunner()).last_usage is None


def test_last_usage_carries_the_four_scalar_counts_and_the_cost():
    """last_usage = env["usage"] | {"cost_usd"}. A real capture shows `usage` is NOT a
    flat scalar map, so this pins BOTH halves: the four scalar token
    counts a consumer must SUM for cost.provider_tokens, and the nested members that prove
    shipping the blob as a single number would be wrong."""
    runner = FakeRunner(_Proc(stdout=_envelope()))
    client = ClaudeCliInferenceClient(runner=runner, output_format="json")
    client.complete("S", "U")
    usage = client.last_usage
    assert usage["input_tokens"] == 181
    assert usage["output_tokens"] == 4
    assert usage["cache_read_input_tokens"] == 0
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cost_usd"] == 0.000603
    assert sum(usage[k] for k in ("input_tokens", "output_tokens",
                                  "cache_read_input_tokens",
                                  "cache_creation_input_tokens")) == 185
    assert isinstance(usage["server_tool_use"], dict)     # nested, not scalar
    assert isinstance(usage["iterations"], list)          # a LIST, not a scalar


def test_last_usage_is_cleared_by_a_failing_call():
    """A caller reading last_usage after a failure must not be handed the PREVIOUS call's
    tokens and cost — that is how a query silently reports another query's spend."""
    runner = FakeRunner(_Proc(stdout=_envelope()))
    client = ClaudeCliInferenceClient(runner=runner, output_format="json")
    client.complete("S", "U")
    assert client.last_usage is not None
    runner.proc = _Proc(returncode=1, stdout="boom")
    with pytest.raises(InferenceError):
        client.complete("S", "U")
    assert client.last_usage is None


def test_last_usage_is_cleared_by_a_transport_failure(monkeypatch):
    """Same property as test_last_usage_is_cleared_by_a_failing_call, but for the two
    paths that never reach a returncode at all: subprocess.TimeoutExpired and
    FileNotFoundError, both caught around `self._run(...)` in complete() and re-raised as
    InferenceError. Each iteration primes last_usage with a REAL successful call first, so
    the assertion after the failure is about CLEARING a previous call's tokens and cost,
    not about last_usage merely never having been set.

    This is the mutation the reviewer ran: moving `self.last_usage = None` from the first
    statement of complete() to immediately after the try/except around `self._run(...)`
    leaves it unreached by both of these except branches (they fire from inside that same
    try), so a caller catching InferenceError here would still read the PRIOR call's cost
    -- exactly the silent cross-query billing bug per-query cost tracing depends on this
    not being."""
    runner = FakeRunner(_Proc(stdout=_envelope()))
    client = ClaudeCliInferenceClient(runner=runner, output_format="json")

    def succeed(args, stdin):
        return runner.proc

    def timed_out(args, stdin):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=0)

    def not_found(args, stdin):
        raise FileNotFoundError("claude")

    for fail in (timed_out, not_found):
        monkeypatch.setattr(client, "_run", succeed)
        client.complete("S", "U")
        assert client.last_usage is not None

        monkeypatch.setattr(client, "_run", fail)
        with pytest.raises(InferenceError):
            client.complete("S", "U")
        assert client.last_usage is None


def test_text_mode_never_populates_last_usage():
    runner = FakeRunner(_Proc(stdout="the reply"))
    client = ClaudeCliInferenceClient(runner=runner)
    client.complete("S", "U")
    assert client.last_usage is None


# ---------------------------------------------------------------------------
# errors: the quota text must survive into InferenceError, unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("returncode", [0, 1])
def test_is_error_envelope_raises_carrying_the_quota_text_verbatim(returncode):
    """UNVERIFIED which exit code the CLI actually uses for a session limit in JSON mode —
    every observed occurrence was text mode (exit 1). Both are handled, and the
    returncode=0 parameter is the one that matters: without an is_error check, an exit-0
    error envelope is returned to the caller as if the MODEL had said 'You've hit your
    session limit', and the breaker never fires."""
    runner = FakeRunner(_Proc(returncode=returncode,
                              stdout=_envelope(is_error=True, subtype="error",
                                               result=QUOTA)))
    client = ClaudeCliInferenceClient(runner=runner, output_format="json")
    with pytest.raises(InferenceError) as exc:
        client.complete("S", "U")
    assert QUOTA in str(exc.value)
    assert "is_error" not in str(exc.value)     # the blob is not the message
    assert client.last_usage is None            # a failed call reports no cost


def test_json_mode_nonzero_exit_message_is_byte_identical_to_text_mode():
    """The error-message contract, as an equality rather than a substring. A breaker
    parses this string; it must not have to know which output mode produced it."""
    text_runner = FakeRunner(_Proc(returncode=1, stdout=QUOTA))
    with pytest.raises(InferenceError) as text_exc:
        ClaudeCliInferenceClient(runner=text_runner).complete("S", "U")

    json_runner = FakeRunner(_Proc(returncode=1,
                                   stdout=_envelope(is_error=True, subtype="error",
                                                    result=QUOTA)))
    with pytest.raises(InferenceError) as json_exc:
        ClaudeCliInferenceClient(runner=json_runner,
                                 output_format="json").complete("S", "U")

    assert str(json_exc.value) == str(text_exc.value)
    assert str(json_exc.value) == f"claude CLI exited 1: {QUOTA}"


def test_a_nonzero_exit_with_a_non_json_body_still_reads_like_text_mode():
    """The CLI may fail before it can print an envelope at all. That must degrade to text
    mode's exact message, not to a parse complaint that buries the real cause."""
    runner = FakeRunner(_Proc(returncode=1, stdout=QUOTA))
    client = ClaudeCliInferenceClient(runner=runner, output_format="json")
    with pytest.raises(InferenceError) as exc:
        client.complete("S", "U")
    assert str(exc.value) == f"claude CLI exited 1: {QUOTA}"


@pytest.mark.parametrize("stdout", [
    "not json at all",                            # not JSON
    "[1, 2]",                                     # valid JSON, wrong top-level type
    '{"type": "result", "is_error": false}',      # a dict with no "result" string
])
def test_a_malformed_envelope_raises_inference_error_never_a_decode_error(stdout):
    runner = FakeRunner(_Proc(stdout=stdout))
    client = ClaudeCliInferenceClient(runner=runner, output_format="json")
    with pytest.raises(InferenceError) as exc:
        client.complete("S", "U")
    assert "--output-format json" in str(exc.value)
    # json.JSONDecodeError is a ValueError; InferenceError is a RuntimeError
    # (inference.InferenceError). If a raw decode error escaped, pytest.raises(InferenceError)
    # above would not have caught it at all — and this line pins the parentage the
    # distinction rests on.
    assert not isinstance(exc.value, ValueError)
    assert client.last_usage is None


def test_a_fenced_envelope_is_an_error_not_something_to_unwrap():
    """The DECISION, pinned. The outer envelope is machine-written by the CLI and is never
    fenced, so stripping fences from it could only ever mask a real transport fault."""
    runner = FakeRunner(_Proc(stdout="```json\n" + _envelope() + "\n```"))
    client = ClaudeCliInferenceClient(runner=runner, output_format="json")
    with pytest.raises(InferenceError, match="unparseable"):
        client.complete("S", "U")


def test_json_mode_unwraps_a_fenced_result_only_on_a_schema_call():
    """env["result"] is the MODEL's own text, and the model still intermittently wraps
    schema replies in ```json fences (claude_cli_client._strip_fences) whatever the transport is.
    So the same rule applies under the same condition, and a JSON-mode reply is byte-
    identical to the text-mode reply for the same model output."""
    fenced = '```json\n{"ok": true}\n```'
    schema_runner = FakeRunner(_Proc(stdout=_envelope(result=fenced)))
    got = ClaudeCliInferenceClient(runner=schema_runner, output_format="json").complete(
        "S", "U", schema={"type": "object"})
    assert got == '{"ok": true}'

    prose_runner = FakeRunner(_Proc(stdout=_envelope(result=fenced)))
    verbatim = ClaudeCliInferenceClient(runner=prose_runner,
                                        output_format="json").complete("S", "U")
    assert verbatim == fenced          # no schema -> prose is never mangled


# ---------------------------------------------------------------------------
# the factory passthrough
# ---------------------------------------------------------------------------

def test_factory_defaults_leave_the_cli_provider_in_text_mode(monkeypatch):
    client = build_inference_client({"KB_INFERENCE_PROVIDER": "claude-cli"})
    assert "--output-format" not in _capture_argv(client, monkeypatch, stdout="ok")


def test_factory_forwards_output_format_to_the_cli_branch(monkeypatch):
    client = build_inference_client({"KB_INFERENCE_PROVIDER": "claude-cli"},
                                    output_format="json")
    argv = _capture_argv(client, monkeypatch, stdout=_envelope())
    assert argv[argv.index("--output-format") + 1] == "json"


def test_factory_forwards_timeout_to_the_cli_branch(monkeypatch):
    """A per-call LLM_CALL_TIMEOUT_S = 45 explicitly overrides the inherited
    KB_INFERENCE_TIMEOUT=300. Before this passthrough there was no way to express it."""
    client = build_inference_client({"KB_INFERENCE_PROVIDER": "claude-cli",
                                     "KB_INFERENCE_TIMEOUT": "300"}, timeout=45.0)
    assert _timeout_message(client, monkeypatch) == "claude CLI timed out after 45.0s"


def test_factory_blank_timeout_env_falls_back_to_the_default(monkeypatch):
    """A launcher forwarding ${KB_INFERENCE_TIMEOUT:-} sets the var to '' when the user did
    not override it. float('') raised ValueError at construction before this task — the same
    trap the model var already guards against (in build_inference_client)."""
    client = build_inference_client({"KB_INFERENCE_PROVIDER": "claude-cli",
                                     "KB_INFERENCE_TIMEOUT": ""})
    assert _timeout_message(client, monkeypatch) == "claude CLI timed out after 300.0s"


def test_factory_does_not_forward_the_cli_only_kwargs_to_the_local_provider(monkeypatch):
    monkeypatch.setenv("KB_INFERENCE_TIMEOUT", "300")
    client = build_inference_client({}, output_format="json", timeout=45.0)
    assert isinstance(client, HTTPInferenceClient)
    assert not hasattr(client, "_output_format")
    # HTTPInferenceClient reads KB_INFERENCE_TIMEOUT itself (in its __init__). Forwarding
    # `timeout` here would give one setting two sources of truth, so it is deliberately not
    # forwarded — and 300.0 rather than 45.0 is what proves that.
    assert client._timeout == 300.0


def test_factory_does_not_forward_the_cli_only_kwargs_to_the_anthropic_provider():
    client = build_inference_client({"KB_INFERENCE_PROVIDER": "anthropic"},
                                    output_format="json", timeout=45.0)
    assert isinstance(client, AnthropicInferenceClient)
    # No subprocess, no argv, no CLI envelope: the flags are meaningless here and forwarding
    # them would imply a usage/cost guarantee this path does not make.
    assert not hasattr(client, "_output_format")
