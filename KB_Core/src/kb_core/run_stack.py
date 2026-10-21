"""kb-run: host-native foreground supervisor — the supported cross-platform way
to run the service fleet (launchd/systemd are optional run-at-boot recipes).
Each service is `bash scripts/kb-svc.sh <name>`; kb-run adds restart-with-
backoff, line-prefixed output, and clean shutdown.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_SERVICES = (
    "backend", "frontend", "inference", "ingest",
    "reanalyze", "resolve", "consolidate", "mcp",
)
# `kb-run --tenant <slug>`: the per-tenant daemon set (matches kb-dev's TENANT_SERVICES).
TENANT_SERVICES = ("ingest", "reanalyze", "resolve", "consolidate")
_BACKOFF_START, _BACKOFF_CAP = 2, 30
_STOP_GRACE = 10   # seconds after a stop signal before escalating stragglers to SIGKILL
_FINAL_JOIN = 5    # seconds to let pump threads observe a SIGKILL'd child actually exit


def _repo_root() -> Path:
    # KB_Core/src/kb_core/run_stack.py -> parents[3] is the superproject root
    # (the directory holding KB_Core, KB_Frontend, KB_Inference, KB_MCP as
    # siblings), wherever this repo has actually been cloned.
    return Path(__file__).resolve().parents[3]


def _svc_script() -> str:
    override = os.environ.get("KB_RUN_SVC_SCRIPT")
    if override:
        return override
    return str(Path(__file__).resolve().parents[2] / "scripts" / "kb-svc.sh")


def _env_file_value(key: str) -> str | None:
    """Read a single `KEY=VALUE` line from `<repo_root>/KB_Core/.env`, if present.

    `.env` is normally sourced into the *children* kb-run spawns (kb-svc.sh does
    `set -a; source .env; set +a`), never into kb-run's own process — so
    `os.environ` alone can't see it here. This is a minimal, non-shell parse
    (strip quotes/whitespace, skip comments/blank lines) used only for the
    ollama preflight below; it deliberately does not source or evaluate the
    file.
    """
    env_path = _repo_root() / "KB_Core" / ".env"
    if not env_path.exists():
        return None
    try:
        lines = env_path.read_text().splitlines()
    except OSError:
        return None
    prefix = f"{key}="
    value: str | None = None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
    return value


def _resolve_ollama_model() -> str:
    # Precedence: an explicit process env var beats KB_Core/.env, which beats
    # the hardcoded default — same order the README documents for kb-run.
    return (os.environ.get("OLLAMA_MODEL")
            or _env_file_value("OLLAMA_MODEL")
            or "gemma4:12b-it-q8_0")


def _child_env() -> dict[str, str]:
    """Env for each spawned `kb-svc.sh` child.

    KB_SUPERVISOR is always forced to "none" (healthcheck reads it). KB_REPO /
    KB_FRONTEND / KB_INFERENCE / KB_MCP_DIR are defaulted from this file's own
    on-disk location, but only when the caller hasn't already set them. kb-svc.sh
    derives the same defaults from its own location; passing them explicitly keeps
    every child on the checkout kb-run itself was started from.
    """
    root = _repo_root()
    env = {**os.environ, "KB_SUPERVISOR": "none"}
    for key, subdir in (
        ("KB_REPO", "KB_Core"),
        ("KB_FRONTEND", "KB_Frontend"),
        ("KB_INFERENCE", "KB_Inference"),
        ("KB_MCP_DIR", "KB_MCP"),
    ):
        if key not in os.environ:
            env[key] = str(root / subdir)
    return env


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


class _Runner:
    def __init__(self, services: list[str]):
        self.services = services
        self.stopping = threading.Event()
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def _pump(self, name: str) -> None:
        backoff = _BACKOFF_START
        while not self.stopping.is_set():
            proc = subprocess.Popen(
                ["bash", _svc_script(), name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env=_child_env(), start_new_session=True,
            )
            with self.lock:
                self.procs[name] = proc
                # Close the race with _stop(): it sets `stopping` before taking
                # the lock, so if we registered after its signal sweep already
                # ran, self-signal here rather than leaking a process group
                # that nothing will ever signal again.
                if self.stopping.is_set():
                    _signal_group(proc, signal.SIGTERM)
            for line in proc.stdout:  # type: ignore[union-attr]
                print(f"[{name}] {line}", end="", flush=True)
            proc.wait()
            if self.stopping.is_set():
                return
            print(f"[kb-run] restarting {name} in {backoff}s (exit {proc.returncode})",
                  flush=True)
            if self.stopping.wait(backoff):
                return
            backoff = min(backoff * 2, _BACKOFF_CAP)

    def run(self) -> int:
        threads = [threading.Thread(target=self._pump, args=(s,), daemon=True)
                   for s in self.services]
        for t in threads:
            t.start()

        def _stop(signum, frame):
            # Signal handlers run on the main thread and can re-enter this
            # function mid-flight -- including while the main thread is
            # itself holding self.lock, here or in the straggler sweep
            # below (an impatient second Ctrl-C/kill is the most likely way
            # to land exactly there). threading.Lock is NOT reentrant, so a
            # naive re-entry would try to re-acquire a lock this same
            # thread already holds and hang forever, defeating the whole
            # point of a shutdown handler. `stopping` is only ever set here
            # (before the lock is touched), so once it's set every later
            # invocation is a guaranteed-safe no-op: the first pass already
            # signals every known proc. Do NOT remove this guard "to
            # simplify" -- it is load-bearing.
            if self.stopping.is_set():
                return
            self.stopping.set()
            with self.lock:
                for proc in self.procs.values():
                    if proc.poll() is None:
                        # Signal the whole process group, not just the direct
                        # child: kb-svc.sh's own TERM trap is installed only
                        # inside run(), so during its pre-run phases (uv sync,
                        # npm install/build) a plain proc.terminate() would
                        # only hit bash while an install-phase grandchild
                        # survives, holding the stdout pipe open forever.
                        _signal_group(proc, signal.SIGTERM)

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        if hasattr(signal, "SIGHUP"):
            # Without a controlling terminal (see start_new_session=True on
            # each child below), closing the terminal window/tab that
            # launched kb-run sends it SIGHUP, not SIGINT/SIGTERM. Handle it
            # the same way, or an ordinary "close the tab" orphans the whole
            # fleet. Guarded for platforms without SIGHUP (e.g. Windows).
            signal.signal(signal.SIGHUP, _stop)
        while not self.stopping.is_set():
            time.sleep(0.2)

        # Shared deadline across all threads, not `_STOP_GRACE` each — with N
        # services a per-thread timeout would let shutdown take up to N times
        # as long as intended.
        deadline = time.monotonic() + _STOP_GRACE
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))

        stragglers = []
        with self.lock:
            for name, proc in self.procs.items():
                if proc.poll() is None:
                    stragglers.append(name)
                    _signal_group(proc, signal.SIGKILL)
        if stragglers:
            print(f"[kb-run] escalating to SIGKILL: {', '.join(stragglers)}", flush=True)
            for t in threads:
                t.join(timeout=_FINAL_JOIN)

        return 0


def main_with(argv: list[str] | None) -> int:
    ap = argparse.ArgumentParser(description="Run the KB service fleet in the foreground.")
    ap.add_argument("--only", help="comma-separated service subset")
    ap.add_argument("--except", dest="excluded", help="comma-separated services to skip")
    ap.add_argument("--tenant", help="run ONE tenant's daemon set (KB_TENANT=<slug>)")
    args = ap.parse_args(argv)
    if args.tenant:
        # Children inherit it via _child_env(); kb-svc.sh's tenant mode does the rest.
        os.environ["KB_TENANT"] = args.tenant
        services = list(args.only.split(",")) if args.only else list(TENANT_SERVICES)
    else:
        services = list(args.only.split(",")) if args.only else list(DEFAULT_SERVICES)
    if args.excluded:
        skip = set(args.excluded.split(","))
        services = [s for s in services if s not in skip]
    if "inference" in services and not os.environ.get("KB_RUN_SKIP_PREFLIGHT"):
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        for model in (_resolve_ollama_model(), "qwen3-embedding:0.6b"):
            try:
                import json as _json
                import urllib.request

                tags = _json.load(urllib.request.urlopen(f"{base}/api/tags", timeout=5))
                names = {m.get("name") for m in tags.get("models", [])}
                if model not in names:
                    print(f"[kb-run] pulling ollama model {model} (first run only)")
                    subprocess.run(["ollama", "pull", model], check=False)
            except Exception as exc:  # ollama down: inference service will report it
                print(f"[kb-run] ollama preflight skipped: {exc}")
                break

    print(f"[kb-run] starting: {', '.join(services)} (Ctrl-C stops everything)")
    return _Runner(services).run()


def main() -> int:
    return main_with(None)


if __name__ == "__main__":
    sys.exit(main())
