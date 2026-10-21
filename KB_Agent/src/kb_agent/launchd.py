"""Install/remove the per-user LaunchAgent that keeps kb-agent running at login.

`program` must start with the Python interpreter itself (`python -m kb_agent.cli run`), never
a shebang script: launchd exec'ing a shebang script that lives under ~/Desktop or ~/Documents
is TCC-denied on macOS and crash-loops.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import time
from pathlib import Path

LABEL = "com.kb.agent"
AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "kb-agent"
LAUNCHCTL_TIMEOUT_S = 30


def plist_path() -> Path:
    return AGENT_DIR / f"{LABEL}.plist"


def render(program: list[str], path_dirs: list[str] | tuple = ()) -> str:
    """`path_dirs` lead PATH: `init` passes claude's own directory, which for an nvm install is
    also where its `node` lives."""
    base = f"/opt/homebrew/bin:{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    env = {"PATH": ":".join([*path_dirs, base])}
    if os.environ.get("KB_AGENT_HOME"):            # `run` must read the config `init` wrote
        env["KB_AGENT_HOME"] = os.environ["KB_AGENT_HOME"]
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": list(program),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        # Only what escapes logging (a crash before it is set up, an interpreter traceback):
        # agent.log is the agent's own rotating file, and a second writer holding an fd on it
        # would keep writing to the rotated-away copy.
        "StandardOutPath": str(LOG_DIR / "launchd.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.log"),
        "EnvironmentVariables": env,
    }).decode()


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=LAUNCHCTL_TIMEOUT_S)


def install(program: list[str], path_dirs: list[str] | tuple = ()) -> Path:
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    p = plist_path()
    p.write_text(render(program, path_dirs))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")              # absent on first install: ignored
    # bootout returns before the old job is gone, and bootstrap then fails with EIO: retry.
    for attempt in range(3):
        r = _launchctl("bootstrap", domain, str(p))
        if r.returncode == 0:
            return p
        time.sleep(1 + attempt)
    raise RuntimeError(f"launchctl bootstrap failed ({r.returncode}): {(r.stderr or '').strip()[:200]}")


def uninstall() -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    plist_path().unlink(missing_ok=True)
