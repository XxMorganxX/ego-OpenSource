import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kb_core import run_stack

STUB = """#!/bin/bash
# stub kb-svc: 'flaky' exits once then runs; others run until killed
name="$1"
echo "start $name"
echo "envcheck KB_REPO=$KB_REPO KB_FRONTEND=$KB_FRONTEND KB_INFERENCE=$KB_INFERENCE KB_MCP_DIR=$KB_MCP_DIR"
echo $$ > "$STUB_STATE/$name.pid"
if [[ "$name" == "flaky" && ! -f "$STUB_STATE/flaky-ran" ]]; then
  touch "$STUB_STATE/flaky-ran"; exit 1
fi
trap 'exit 0' TERM INT
while true; do sleep 0.2; done
"""


def test_env_file_value_reads_key_ignores_comments_and_strips_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(run_stack, "_repo_root", lambda: tmp_path)
    (tmp_path / "KB_Core").mkdir()
    (tmp_path / "KB_Core" / ".env").write_text(
        "# a comment, ignored\n"
        "\n"
        "OTHER_VAR=not-this-one\n"
        "OLLAMA_MODEL=gemma4:26b\n"
        "QUOTED_VAR=\"has spaces\"\n"
    )
    assert run_stack._env_file_value("OLLAMA_MODEL") == "gemma4:26b"
    assert run_stack._env_file_value("QUOTED_VAR") == "has spaces"
    assert run_stack._env_file_value("MISSING_VAR") is None


def test_env_file_value_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(run_stack, "_repo_root", lambda: tmp_path)
    assert run_stack._env_file_value("OLLAMA_MODEL") is None


def test_env_file_value_last_matching_line_wins(tmp_path, monkeypatch):
    # Mirrors `source`/`tail -1` semantics for a key set twice in one .env file.
    monkeypatch.setattr(run_stack, "_repo_root", lambda: tmp_path)
    (tmp_path / "KB_Core").mkdir()
    (tmp_path / "KB_Core" / ".env").write_text("OLLAMA_MODEL=first\nOLLAMA_MODEL=second\n")
    assert run_stack._env_file_value("OLLAMA_MODEL") == "second"


def test_resolve_ollama_model_env_var_overrides_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_stack, "_repo_root", lambda: tmp_path)
    (tmp_path / "KB_Core").mkdir()
    (tmp_path / "KB_Core" / ".env").write_text("OLLAMA_MODEL=from-env-file\n")
    monkeypatch.setenv("OLLAMA_MODEL", "from-process-env")
    assert run_stack._resolve_ollama_model() == "from-process-env"


def test_resolve_ollama_model_falls_back_to_env_file_then_default(tmp_path, monkeypatch):
    monkeypatch.setattr(run_stack, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    (tmp_path / "KB_Core").mkdir()
    (tmp_path / "KB_Core" / ".env").write_text("OLLAMA_MODEL=from-env-file\n")
    assert run_stack._resolve_ollama_model() == "from-env-file"

    (tmp_path / "KB_Core" / ".env").unlink()
    assert run_stack._resolve_ollama_model() == "gemma4:12b-it-q8_0"


def _launch(tmp_path, services):
    stub = tmp_path / "stub-svc.sh"
    stub.write_text(STUB)
    stub.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "STUB_STATE": str(tmp_path),
           "KB_RUN_SVC_SCRIPT": str(stub),
           # None of these services is "inference", so the ollama preflight
           # never triggers today -- pin it off anyway so this suite stays
           # hermetic if that changes (no live /api/tags probe, no `ollama
           # pull` from a test run).
           "KB_RUN_SKIP_PREFLIGHT": "1"}
    return subprocess.Popen(
        [sys.executable, "-m", "kb_core.run_stack", "--only", services],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        cwd=Path(__file__).parents[1],
    )


def _wait_for_pid(tmp_path, name, deadline_s=10):
    pid_file = tmp_path / f"{name}.pid"
    deadline = time.monotonic() + deadline_s
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert pid_file.exists(), f"stub never reported its pid for {name}"
    return int(pid_file.read_text().strip())


def test_prefixes_restarts_and_clean_shutdown(tmp_path):
    proc = _launch(tmp_path, "steady,flaky")
    time.sleep(4)  # enough for flaky's exit + first backoff restart
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 0
    assert "[steady] start steady" in out
    assert out.count("start flaky") >= 2          # restarted after exit
    assert "restarting flaky" in out
    # kb-run derives KB_REPO/KB_FRONTEND/KB_INFERENCE/KB_MCP_DIR from its own
    # on-disk location when the launching env hasn't already set them (a
    # clone can live anywhere). The env dict in _launch()
    # above deliberately omits them, so this exercises the "unset" path.
    root = Path(__file__).resolve().parents[2]
    assert (
        f"envcheck KB_REPO={root / 'KB_Core'} "
        f"KB_FRONTEND={root / 'KB_Frontend'} "
        f"KB_INFERENCE={root / 'KB_Inference'} "
        f"KB_MCP_DIR={root / 'KB_MCP'}"
    ) in out


def test_sigterm_exits_within_deadline_and_kills_children(tmp_path):
    proc = _launch(tmp_path, "steady")
    child_pid = _wait_for_pid(tmp_path, "steady")

    start = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=20)
    elapsed = time.monotonic() - start

    assert proc.returncode == 0
    assert elapsed <= 16, f"shutdown took {elapsed:.1f}s (expected within ~15s)"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_double_sigterm_is_idempotent(tmp_path):
    """A second SIGTERM landing mid-shutdown must not hang kb-run.

    Review round 2 found a self-deadlock: _stop() is a signal handler that
    runs on (and can re-enter) the main thread, including while that same
    thread already holds self.lock (in _stop() itself, or in run()'s
    post-deadline straggler sweep) -- threading.Lock isn't reentrant, so an
    unguarded re-entry there would hang forever. The fix is an
    `if self.stopping.is_set(): return` guard as _stop()'s first statement.

    This test cannot deterministically land the second SIGTERM on the exact
    instant the main thread holds the lock -- Python signal delivery timing
    isn't controllable from here, so it doesn't prove the race is hit every
    run. What it does pin down: sending a second SIGTERM partway through an
    already-in-progress shutdown must still result in a clean exit with
    every child actually dead, on whatever timing this run happens to get.
    Before the guard, an unlucky run landing in the lock-held window would
    have hung this test at `communicate(timeout=20)` instead of passing.
    """
    proc = _launch(tmp_path, "steady")
    child_pid = _wait_for_pid(tmp_path, "steady")

    start = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    time.sleep(0.2)          # land the 2nd signal inside the shutdown window
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=20)
    elapsed = time.monotonic() - start

    assert proc.returncode == 0
    assert elapsed <= 16, f"shutdown took {elapsed:.1f}s (expected within ~15s)"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP is POSIX-only")
def test_sighup_triggers_clean_shutdown(tmp_path):
    proc = _launch(tmp_path, "steady")
    child_pid = _wait_for_pid(tmp_path, "steady")

    start = time.monotonic()
    proc.send_signal(signal.SIGHUP)
    out, _ = proc.communicate(timeout=20)
    elapsed = time.monotonic() - start

    assert proc.returncode == 0
    assert elapsed <= 16, f"shutdown took {elapsed:.1f}s (expected within ~15s)"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_run_stack_tenant_flag_selects_tenant_daemons_and_sets_env(monkeypatch):
    captured = {}

    class _FakeRunner:
        # Mirrors _Runner's real signature (services only). Each child's env is
        # _child_env(), built from os.environ at spawn time — capture it that way.
        def __init__(self, services):
            captured["services"] = list(services)
            captured["env"] = dict(run_stack._child_env())

        def run(self):
            return 0

    monkeypatch.setattr(run_stack, "_Runner", _FakeRunner)
    monkeypatch.setenv("KB_RUN_SKIP_PREFLIGHT", "1")
    # main_with writes os.environ["KB_TENANT"]; register the key so monkeypatch restores it.
    monkeypatch.setenv("KB_TENANT", "owner")
    assert run_stack.main_with(["--tenant", "alice"]) == 0
    assert captured["services"] == ["ingest", "reanalyze", "resolve", "consolidate"]
    assert captured["env"]["KB_TENANT"] == "alice"
