"""Health of the Claude credential that the claude-cli inference provider runs on.

Consumed by BOTH the dashboard API and healthcheck, so the banner and the push
notification can never disagree about whether auth is fine.

WATCHES `refreshTokenExpiresAt`, NOT `expiresAt`. The access token expires roughly
every 6 hours and refreshes itself silently; alerting on it would fire several times
a day for a condition that resolves with nobody doing anything, and the banner would
be trained-out inside a week. Only the REFRESH token expiring (~29 day cycle) needs
a human to run `claude auth login`.
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import time

CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
WARN_DAYS = 7.0
CRITICAL_DAYS = 2.0
_CLI_TIMEOUT = 5


def _cli_status() -> "dict | None":
    """`claude auth status` parsed, or None if the CLI could not be consulted.

    Bounded by a timeout because this runs inside a dashboard request handler: an
    unbounded external binary that hangs would wedge the API.
    """
    try:
        p = subprocess.run(["claude", "auth", "status"], capture_output=True,
                           text=True, timeout=_CLI_TIMEOUT)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None


def _refresh_expiry_ms(path: Path) -> "int | None":
    """claudeAiOauth.refreshTokenExpiresAt in epoch ms, or None if unreadable.

    This file is Claude Code's PRIVATE format with no compatibility promise, so every
    failure mode collapses to None: a guessed date is worse than an absent one,
    because the banner's entire value is that its number can be trusted.
    """
    try:
        return int(json.loads(path.read_text())["claudeAiOauth"]["refreshTokenExpiresAt"])
    except Exception:
        return None


def claude_cli_in_use(env: "dict | None" = None) -> bool:
    """Whether inference.toml routes any task to claude-cli — the only thing this
    credential is for. With none (an Ollama-only install) a signed-out CLI strands
    nothing, so the banner and the healthcheck stay quiet and `claude` is never run.
    An unreadable routing file answers True: unsure means keep watching."""
    try:
        from kb_core.inference_routing import load_routing
        return any(r.provider == "claude-cli" for r in load_routing(env).values())
    except Exception:
        return True


# What the banner and healthcheck report when no task routes to claude-cli.
NOT_IN_USE = {"level": "ok", "logged_in": None, "days_left": None, "expires_at": None,
              "email": None, "in_use": False}


def claude_auth_status(credentials_path: Path = CREDENTIALS,
                       now: "float | None" = None) -> dict:
    """{level, logged_in, days_left, expires_at, email} — never any token."""
    now = time.time() if now is None else now
    cli = _cli_status()
    if cli is None:
        return {"level": "out", "logged_in": False, "days_left": None,
                "expires_at": None, "email": None}

    logged_in = bool(cli.get("loggedIn"))
    ms = _refresh_expiry_ms(credentials_path)
    days_left = None if ms is None else (ms / 1000 - now) / 86400
    expires_at = (None if ms is None else
                  datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat())

    if not logged_in:
        level = "out"
    elif days_left is None:
        level = "ok"                     # cannot measure -> say nothing
    elif days_left < CRITICAL_DAYS:
        level = "critical"
    elif days_left < WARN_DAYS:
        level = "warn"
    else:
        level = "ok"

    return {"level": level, "logged_in": logged_in, "days_left": days_left,
            "expires_at": expires_at, "email": cli.get("email")}
