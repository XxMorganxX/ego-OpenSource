"""`claude -p` runs with live filesystem tools unless told otherwise.

Verified by hand: without `--tools ""`, print mode really does execute Read on a
relative path in its cwd, unprompted. The neutral temp cwd (_ensure_cwd) constrains
relative paths only. Since claude-cli is a production provider (inference.toml routes
enrichment tasks to it), every such pass feeds raw note text to a model holding those tools.

These tests assert on the constructed argv rather than spawning a subprocess, so they
are fast and hermetic.
"""

from kb_core.ingest.claude_cli_client import ClaudeCliInferenceClient

HARDENING = ["--tools", "",
             "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--setting-sources", ""]


def _argv(client, monkeypatch, schema=None):
    """Capture the argv the client would spawn, without spawning it."""
    seen = {}

    def fake_run(args, user_prompt):
        seen["args"] = args
        class P:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return P()

    monkeypatch.setattr(client, "_run", fake_run)
    client.complete("sys", "user", schema=schema)
    return seen["args"]


def test_the_client_is_hardened_by_default(monkeypatch):
    """A caller constructing the client directly (not via build_inference_client) must
    not get a tool-holding `claude -p`."""
    argv = _argv(ClaudeCliInferenceClient(), monkeypatch)
    assert argv[-len(HARDENING):] == HARDENING


def test_opting_out_drops_every_hardening_flag(monkeypatch):
    argv = _argv(ClaudeCliInferenceClient(hardened=False), monkeypatch)
    for flag in ("--tools", "--strict-mcp-config", "--mcp-config", "--setting-sources"):
        assert flag not in argv


def test_hardened_disables_tools(monkeypatch):
    argv = _argv(ClaudeCliInferenceClient(hardened=True), monkeypatch)
    i = argv.index("--tools")
    assert argv[i + 1] == ""


def test_hardened_disables_mcp_so_the_model_cannot_reach_the_kb_itself(monkeypatch):
    argv = _argv(ClaudeCliInferenceClient(hardened=True), monkeypatch)
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'


def test_hardened_disables_setting_sources(monkeypatch):
    argv = _argv(ClaudeCliInferenceClient(hardened=True), monkeypatch)
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_hardening_appends_and_preserves_the_base_argv(monkeypatch):
    base = _argv(ClaudeCliInferenceClient(hardened=False), monkeypatch)
    hard = _argv(ClaudeCliInferenceClient(hardened=True), monkeypatch)
    assert hard[:len(base)] == base
    assert hard[len(base):] == HARDENING


def test_hardening_survives_a_schema_call(monkeypatch):
    """schema= rewrites the system prompt; the flags must still be present."""
    argv = _argv(ClaudeCliInferenceClient(hardened=True), monkeypatch)
    schema_argv = _argv(ClaudeCliInferenceClient(hardened=True), monkeypatch,
                         schema={"type": "object"})
    assert "--tools" in argv and "--tools" in schema_argv


def test_build_inference_client_hardens_the_cli_provider_by_default(monkeypatch):
    """Enrichment feeds untrusted corpus text to the model, so the provider it
    actually gets in production must be the hardened one."""
    from kb_core.ingest.inference import build_inference_client

    client = build_inference_client({"KB_INFERENCE_PROVIDER": "claude-cli"})
    argv = _argv(client, monkeypatch)
    assert argv[argv.index("--tools") + 1] == ""


def test_build_inference_client_can_opt_out(monkeypatch):
    from kb_core.ingest.inference import build_inference_client

    client = build_inference_client({"KB_INFERENCE_PROVIDER": "claude-cli"},
                                    hardened=False)
    assert "--tools" not in _argv(client, monkeypatch)
