"""Run claimed jobs through the tenant's own `claude -p`, hardened (no tools, no MCP, no settings).

Framing matches the owner's kb_core ClaudeCliInferenceClient exactly, so tenant output matches
owner output: the job's system prompt (the server's prompt template, never note content), with
the schema instruction appended, is `--system-prompt`; the user prompt, which carries note text,
goes on stdin only, never argv (argv is visible in `ps`). Nothing here logs prompt or reply text:
job ids, sizes, durations, exit codes.

A copy, not an import: kb_agent imports nothing from kb_core but the stamp module. The flags,
the API-key stripping, the schema instruction and the quota markers mirror kb_core's client;
keep the two in step.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kb_agent.client import GatewayError
from kb_agent.config import AgentConfig

log = logging.getLogger("kb-agent")

# The server's waiter gives a job its window from ENQUEUE (AgentQueueClient timeout_s, 600 s by
# default), so a late claim must finish inside it. The claim carries `deadline_in_s` (seconds
# left at claim, server clock), and each run gets min(JOB_TIMEOUT_S, remaining - REPORT_MARGIN_S);
# with under MIN_REMAINING_S left the job is failed fast, never run. These
# match kb_core.llm_jobs (AGENT_RUN_TIMEOUT_S, MIN_CLAIM_REMAINING_S, STALE_CLAIM_S); a test
# pins them together.
JOB_TIMEOUT_S = 300.0
REPORT_MARGIN_S = 10.0
MIN_REMAINING_S = 15.0
AUTH_TIMEOUT_S = 10.0
DEFAULT_MODEL = "claude-sonnet-5"                 # kb_core's DEFAULT_CLI_MODEL
# A quota or auth failure with no parseable reset (`_parse_reset`): the server defers the node until then
# (AgentQueueClient's pause_minutes default) and this agent stops claiming for as long.
QUOTA_PAUSE = timedelta(minutes=15)
POST_ATTEMPTS = 3
MAX_REPORT_SLEEP_S = 10.0
_RETRYABLE = frozenset({0, 429, 502, 503, 504})
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_AUTH_MARKERS = ("failed to authenticate", "not logged in")
_TRANSIENT_MARKERS = ("hit your session limit", "hit your weekly limit", "usage limit reached", *_AUTH_MARKERS)
# A model name must not read as a flag: no leading dash, no spaces, bounded.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,99}")
# Reset grammars, copied from kb_core's claude_cli_client (a test pins the two equal).
# "resets 5:30am (Europe/Berlin)": the session (5-hour) limit.
_SESSION_RESET_RE = re.compile(r"resets (\d{1,2})(?::(\d{2}))?(am|pm)\s*\(([^)]+)\)", re.I)
# "resets Aug 1 at 5am": the weekly limit. No zone: machine-local.
_WEEKLY_RESET_RE = re.compile(
    r"resets ([A-Z][a-z]{2,8}) (\d{1,2}) at (\d{1,2})(?::(\d{2}))?(am|pm)", re.I)
_HARDENING = ["--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
              "--setting-sources", ""]


class JobError(Exception):
    """A job that could not be completed. `str(exc)` goes to the gateway (the tenant's own
    database); `log_detail` goes to the local log. Neither ever holds prompt or reply text."""

    def __init__(self, message: str, *, log_detail: str | None = None) -> None:
        super().__init__(message)
        self.log_detail = log_detail or message


class ProviderPaused(JobError):
    """Claude is refusing every call for now (quota window, auth). `until` when stated."""

    def __init__(self, message: str, *, until: datetime | None = None, log_detail: str | None = None,
                 auth: bool = False) -> None:
        super().__init__(message, log_detail=log_detail)
        self.until, self.auth = until, auth


def _hour24(hour12: int, ampm: str) -> int:
    return hour12 % 12 + (12 if ampm.lower() == "pm" else 0)


def _parse_reset(detail: str, now: datetime | None = None) -> datetime | None:
    """The reset time a quota message states, tz-aware; None if absent or unparseable. Both
    grammars mean the NEXT occurrence, so a wall time already past rolls forward."""
    now = now or datetime.now(timezone.utc)
    m = _SESSION_RESET_RE.search(detail)
    if m:
        hh, mm, ampm, zone = m.groups()
        try:
            tz = ZoneInfo(zone)
        except Exception:
            return None
        local = now.astimezone(tz)
        candidate = local.replace(hour=_hour24(int(hh), ampm), minute=int(mm or 0), second=0, microsecond=0)
        return candidate if candidate > local else candidate + timedelta(days=1)
    m = _WEEKLY_RESET_RE.search(detail)
    if m:
        mon, day, hh, mm, ampm = m.groups()
        try:
            month = datetime.strptime(mon[:3].title(), "%b").month
        except ValueError:
            return None
        local = now.astimezone(datetime.now(timezone.utc).astimezone().tzinfo)
        try:
            candidate = local.replace(month=month, day=int(day), hour=_hour24(int(hh), ampm),
                                      minute=int(mm or 0), second=0, microsecond=0)
        except ValueError:          # e.g. "resets Feb 30": never trust remote grammar
            return None
        return candidate if candidate > local else candidate.replace(year=candidate.year + 1)
    return None


def _first_line(text: str | None, limit: int = 200) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def _strip_fences(reply: str) -> str:
    """Unwrap a reply fully enclosed in one markdown code fence (kb_core does the same on
    schema calls; the model intermittently fences JSON despite being told not to)."""
    text = reply.strip()
    if text.startswith("```") and text.endswith("```"):
        nl = text.find("\n")
        if nl != -1:
            return text[nl + 1:-3].strip()
    return text


def _env(binary: str = "claude") -> dict:
    """The subscription login, never an API key: these outrank it even when empty. An absolute
    `binary`'s own directory leads PATH: an npm/nvm `claude` is a `#!/usr/bin/env node` script,
    nvm keeps `node` beside it, and launchd's fixed PATH has neither."""
    env = dict(os.environ)
    for var in _API_KEY_VARS:
        env.pop(var, None)
    bin_dir = os.path.dirname(binary)
    if os.path.isabs(binary) and bin_dir:
        env["PATH"] = os.pathsep.join(p for p in (bin_dir, env.get("PATH", "")) if p)
    return env


FALLBACK_TMP = Path("/private/tmp")


def claude_cwd_path() -> Path:
    """`<per-user temp dir>/kb-agent-<uid>/claude-cwd`: fixed, and never under $HOME (why: see
    config's module docstring). A TMPDIR inside $HOME falls back to /private/tmp."""
    base = Path(tempfile.gettempdir())
    try:
        home, resolved = Path.home().resolve(), base.resolve()
        if resolved == home or home in resolved.parents:
            base = FALLBACK_TMP
    except (OSError, RuntimeError):
        base = FALLBACK_TMP
    return base / f"kb-agent-{os.getuid()}" / "claude-cwd"


def _ensure_private_dir(path: Path, *, uid: int | None = None) -> None:
    """Create `path` 0700, or accept it only as a real directory (not a symlink) owned by us:
    in a shared /tmp another user could otherwise plant the directory or a link to elsewhere."""
    uid = os.getuid() if uid is None else uid
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        raise ProviderPaused(f"refusing claude cwd {path}: not a directory owned by uid {uid}")
    os.chmod(path, 0o700)                # mkdir's mode is masked by umask, and ignored if it existed


def _scratch_cwd() -> str:
    """The fixed, empty, 0700 cwd for every `claude` run. An unusable one defers the job
    (ProviderPaused) rather than burn its attempts: it is this Mac's problem, not the job's."""
    d = claude_cwd_path()
    try:
        _ensure_private_dir(d.parent)
        _ensure_private_dir(d)
    except OSError as exc:
        raise ProviderPaused(f"refusing claude cwd {d}: {type(exc).__name__}") from None
    return str(d)


def system_text(system_prompt: str, schema: dict | None) -> str:
    """The `--system-prompt` value, byte-identical to kb_core's ClaudeCliInferenceClient."""
    if schema is None:
        return system_prompt
    return (f"{system_prompt}\n\nReply with ONLY a JSON value conforming to this JSON "
            f"schema — no prose, no markdown fences:\n{json.dumps(schema)}")


class ClaudeCli:
    """One `complete()` = one hardened `claude -p` subprocess. `run` is injectable for tests."""

    def __init__(self, model: str, *, binary: str = "claude", timeout: float = JOB_TIMEOUT_S,
                 run=subprocess.run) -> None:
        self.model, self.binary, self.timeout, self._run = model, binary, timeout, run

    def argv(self, system: str) -> list[str]:
        return [self.binary, "-p", "--model", self.model, "--system-prompt", system,
                "--exclude-dynamic-system-prompt-sections", *_HARDENING]

    def complete(self, system_prompt: str, user_prompt: str, *, schema: dict | None = None) -> str:
        cwd = _scratch_cwd()
        try:
            proc = self._run(self.argv(system_text(system_prompt, schema)), input=user_prompt,
                             capture_output=True, encoding="utf-8", errors="replace",
                             timeout=self.timeout, cwd=cwd, env=_env(self.binary))
        except FileNotFoundError:
            # Not the job's fault and not permanent (an nvm removal, an upgrade's symlink swap):
            # defer it with a retry_after rather than burn its attempts, and have the loop
            # re-check the CLI at once (auth=True drops the cached "logged in").
            raise ProviderPaused(not_found_message(self.binary), auth=True) from None
        except subprocess.TimeoutExpired:
            raise JobError(f"claude timed out after {self.timeout:.0f}s") from None
        if proc.returncode != 0:
            # stdout may be model output, so it never reaches the gateway or the log: only the
            # first stderr line does. Text mode writes quota/auth errors to stdout, so stdout is
            # READ for the transient markers, and the matched marker (our constant) is reported.
            stderr = _first_line(proc.stderr)
            message = (f"claude exited {proc.returncode}: {stderr}" if stderr
                       else f"claude exited {proc.returncode} (no stderr)")
            log_detail = f"exit {proc.returncode}; stderr: {stderr or '<empty>'}"
            both = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
            marker = next((m for m in _TRANSIENT_MARKERS if m in both), None)
            if marker:
                raise ProviderPaused(f"{message}; provider paused: {marker}", log_detail=log_detail,
                                     auth=marker in _AUTH_MARKERS,
                                     until=_parse_reset(f"{proc.stdout or ''}\n{proc.stderr or ''}"))
            raise JobError(message, log_detail=log_detail)
        out = (proc.stdout or "").strip()
        return _strip_fences(out) if schema is not None else out


def not_found_message(path: str) -> str:
    return f"claude CLI not found at {path}; re-run kb-agent init"


def claude_auth_status(*, run=subprocess.run, binary: str = "claude") -> dict:
    """{"logged_in", "email"} from `claude auth status`; logged out whenever it cannot be asked.
    A missing binary is not "logged out": it adds {"error": "not_found", "path": binary}."""
    try:
        p = run([binary, "auth", "status"], capture_output=True, encoding="utf-8", errors="replace",
                timeout=AUTH_TIMEOUT_S, env=_env(binary))
        data = json.loads(p.stdout) if p.returncode == 0 else {}
    except FileNotFoundError:
        return {"logged_in": False, "email": None, "error": "not_found", "path": binary}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"logged_in": bool(data.get("loggedIn")), "email": data.get("email")}


class JobRunner:
    def __init__(self, cfg: AgentConfig, gateway, *, make_client=None, sleep=time.sleep) -> None:
        self.cfg, self.gateway = cfg, gateway
        self._make = make_client or self._default_client
        self._sleep = sleep
        self.paused_until: datetime | None = None       # the loop claims nothing before this
        self.auth_stale = False                          # an auth failure: the loop re-checks the login

    def _default_client(self, model: str):
        return ClaudeCli(model, binary=self.cfg.claude_path or "claude")

    def _model(self, job: dict) -> str:
        for m in (job.get("model_hint"), self.cfg.claude_model):
            if m and _MODEL_RE.fullmatch(m):
                return m
            if m:
                log.warning("job %s: ignoring a malformed model name", job.get("id"))
        return DEFAULT_MODEL

    @staticmethod
    def _remaining(job: dict, received: float) -> float | None:
        """Seconds left of the producer's window, or None when the claim carries none. Counted
        from when the claim reply arrived (`received`), so time queued in run_batch counts."""
        left = job.get("deadline_in_s")
        if isinstance(left, bool) or not isinstance(left, (int, float)):
            return None
        return float(left) - (time.monotonic() - received)

    def run_one(self, job: dict, *, received: float | None = None) -> str:
        """`done` | `failed` | `stale` (the claim was taken back before we reported) |
        `skipped` (too little of the producer's window left to be worth running)."""
        jid, task = job.get("id"), job.get("task")
        started = time.monotonic()
        remaining = self._remaining(job, started if received is None else received)
        if remaining is not None and remaining < MIN_REMAINING_S:
            # Running it would spend the tenant's quota on a reply the producer has stopped
            # waiting for (a 409). Fail fast; the producer expires the job on its own.
            log.info("job %s (%s): %.0fs of its window left, skipped", jid, task, max(remaining, 0.0))
            return self._report(job, self.gateway.post_failure,
                                f"skipped: {max(remaining, 0.0):.0f}s left before the producer's deadline",
                                None) or "skipped"
        try:
            client = self._make(self._model(job))
            if remaining is not None and getattr(client, "timeout", None) is not None:
                client.timeout = min(client.timeout, remaining - REPORT_MARGIN_S)
            reply = client.complete(
                job["system_prompt"], job["user_prompt"], schema=job.get("schema"))
        except ProviderPaused as exc:
            until = exc.until or datetime.now(timezone.utc) + QUOTA_PAUSE
            self.paused_until = until
            if exc.auth:
                self.auth_stale = True
            log.warning("job %s (%s) failed in %.1fs, claude paused until %s: %s",
                        jid, task, time.monotonic() - started, until.isoformat(), exc.log_detail)
            return self._report(job, self.gateway.post_failure, str(exc)[:500], until.isoformat()) or "failed"
        except JobError as exc:
            log.warning("job %s (%s) failed in %.1fs: %s", jid, task, time.monotonic() - started, exc.log_detail)
            return self._report(job, self.gateway.post_failure, str(exc)[:500], None) or "failed"
        except Exception as exc:                          # never let one job kill the loop
            log.warning("job %s (%s) failed in %.1fs: %s", jid, task, time.monotonic() - started, type(exc).__name__)
            return self._report(job, self.gateway.post_failure, type(exc).__name__, None) or "failed"
        log.info("job %s (%s) done in %.1fs: prompt %d B, reply %d B", jid, task, time.monotonic() - started,
                 len(job["system_prompt"].encode()) + len(job["user_prompt"].encode()), len(reply.encode()))
        return self._report(job, self.gateway.post_result, reply) or "done"

    def _report(self, job: dict, post, *args) -> str | None:
        """None once the gateway has it; `stale` on 409; `failed` when it could not be told
        (the server reclaims the claim after kb_core.llm_jobs.STALE_CLAIM_S)."""
        for attempt in range(1, POST_ATTEMPTS + 1):
            try:
                post(job["id"], job["claim_token"], *args)
                return None
            except GatewayError as exc:
                if exc.status == 409:
                    log.info("job %s: claim no longer ours (%s)", job.get("id"), exc.detail)
                    return "stale"
                if exc.status not in _RETRYABLE or attempt == POST_ATTEMPTS:
                    log.warning("job %s: could not report to the gateway: %s", job.get("id"), exc)
                    return "failed"
                # Capped: this sleeps a pool worker, and run_batch blocks claiming until it ends.
                delay = exc.retry_after if exc.retry_after is not None else 2.0 * attempt
                self._sleep(min(delay, MAX_REPORT_SLEEP_S))
        return "failed"

    def run_batch(self, jobs: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        received = time.monotonic()                     # the claim reply just arrived
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.job_concurrency)) as ex:
            for outcome in ex.map(lambda j: self.run_one(j, received=received), jobs):
                counts[outcome] = counts.get(outcome, 0) + 1
        return counts
