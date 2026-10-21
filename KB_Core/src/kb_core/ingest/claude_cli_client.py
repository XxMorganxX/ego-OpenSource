"""Claude Code CLI backend for the inference Protocol — a no-API-key stopgap.

Shells out to `claude -p` (print mode) instead of the HTTP API, so completions run against the
machine's existing Claude Code auth (subscription) rather than metered API credits. Satisfies the
same `InferenceClient.complete()` contract; select it per task with `provider = "claude-cli"`
(or `claude-p`) in inference.toml. Completion only — embeddings still use the local KB_Inference server.

Mapping per pass: the pass's system prompt → `--system-prompt` (which *replaces* Claude Code's
default system prompt, so no coding-agent behaviour leaks in), the rendered user prompt → stdin,
the reply → stdout. Runs in a throwaway temp cwd so the CLI doesn't load a project CLAUDE.md.

Optionally `--output-format json`, which makes stdout the CLI's own result envelope rather
than the reply text; `complete()` then returns `env["result"]` and stashes the call's token
usage and cost on `last_usage`. Off by default: enrichment reads plain stdout as before.
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kb_core.ingest.inference import InferenceError, ProviderUnavailable

# Auth vars that would make `claude -p` use API-key auth and outrank the claude.ai subscription
# login — even when empty. Stripped from the subprocess env so the CLI always uses the login.
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

DEFAULT_CLI_MODEL = "claude-sonnet-5"
DEFAULT_CLI_TIMEOUT = 300.0

# One neutral, empty cwd per PROCESS, shared by every ClaudeCliInferenceClient built
# in it -- not one per instance. Before this, _ensure_cwd's mkdtemp() ran once per
# INSTANCE and nothing ever removed the directory; that was tolerable when one
# process built one long-lived client, but LLMHopJudge._ensure_client
# (KB_Retrieval/crawl/judge.py) was a historical consumer that built a fresh
# ClaudeCliInferenceClient per search_graph call, creating a leaked directory per
# agentic query. The LLM tier has since been deleted; surviving consumers are
# enrichment/analysis, which run long-lived and share this cwd. An install that ran the
# old agentic tier may still have leftover `kb-claude-cli-*` dirs under $TMPDIR; they
# are inert and safe to delete by hand.
#
# The directory is a CWD, not a scratch resource: nothing in this module (or
# `claude -p --tools ""`, the only hardened mode this codebase spawns against
# corpus text) writes into it, so sharing it across concurrent hops/threads is safe
# -- there is no file for two writers to collide on. `atexit` removes it when the
# process exits, which is what actually closes the leak rather than just slowing it
# to "one per process" again.
_shared_cwd_lock = threading.Lock()
_shared_cwd: "str | None" = None


def _process_scratch_cwd() -> str:
    """Lazily create (once per process) and return the shared neutral cwd."""
    global _shared_cwd
    if _shared_cwd is not None:          # fast path: no lock once it's warm
        return _shared_cwd
    with _shared_cwd_lock:
        if _shared_cwd is None:          # re-check: another thread may have won the race
            _shared_cwd = tempfile.mkdtemp(prefix="kb-claude-cli-")
            atexit.register(shutil.rmtree, _shared_cwd, ignore_errors=True)
        return _shared_cwd


def _strip_fences(reply: str) -> str:
    """Unwrap a reply fully enclosed in a markdown code fence (```json ... ``` or bare
    ```). Only the outermost fence is removed; anything not fence-enclosed is returned
    unchanged."""
    text = reply.strip()
    if text.startswith("```") and text.endswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            return text[first_nl + 1 : -3].strip()
    return text


# Transient-outage grammars, observed live (all quota text is authored in THIS file's
# error paths and parsed nowhere else — see _stream_detail's docstring). Detection is
# substring-based and lowercase; reset parsing is best-effort and degrades to None
# (= "re-probe on the default interval"), never to "not transient".
_TRANSIENT_MARKERS = ("hit your session limit", "hit your weekly limit",
                      "usage limit reached", "failed to authenticate", "not logged in")
# "resets 5:30am (Europe/Berlin)" — session (5-hour) limit; the zone is an IANA name.
_SESSION_RESET_RE = re.compile(r"resets (\d{1,2})(?::(\d{2}))?(am|pm)\s*\(([^)]+)\)", re.I)
# "resets Aug 1 at 5am" — weekly limit. No zone: machine-local.
_WEEKLY_RESET_RE = re.compile(
    r"resets ([A-Z][a-z]{2,8}) (\d{1,2}) at (\d{1,2})(?::(\d{2}))?(am|pm)", re.I)


def _hour24(hour12: int, ampm: str) -> int:
    return hour12 % 12 + (12 if ampm.lower() == "pm" else 0)


def _parse_reset(detail: str, now: "datetime | None" = None) -> "datetime | None":
    """The reset time a quota message states, as a tz-aware datetime; None if absent
    or unparseable. `now` is injectable for tests; both grammars mean "the NEXT
    occurrence", so a wall time already past rolls forward (a day / a year)."""
    now = now or datetime.now(timezone.utc)
    m = _SESSION_RESET_RE.search(detail)
    if m:
        hh, mm, ampm, zone = m.groups()
        try:
            tz = ZoneInfo(zone)
        except Exception:
            return None
        local = now.astimezone(tz)
        candidate = local.replace(hour=_hour24(int(hh), ampm), minute=int(mm or 0),
                                  second=0, microsecond=0)
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
            candidate = local.replace(month=month, day=int(day),
                                      hour=_hour24(int(hh), ampm), minute=int(mm or 0),
                                      second=0, microsecond=0)
        except ValueError:          # e.g. "resets Feb 30" — never trust remote grammar
            return None
        return candidate if candidate > local else candidate.replace(year=candidate.year + 1)
    return None


class ClaudeCliInferenceClient:
    """One `complete()` = one `claude -p` subprocess. `runner` is injectable for tests.

    Two modes: `hardened` (drops tools/MCP/settings), default ON, so a caller constructing
    the client directly never hands note text to a `claude -p` holding live tools or its
    MCP servers; pass `hardened=False` to opt out. And `output_format="json"`, default off,
    which asks the CLI for its result envelope so a caller can read the call's provider
    token usage and cost back off `last_usage`. NEITHER forks the subprocess body — the API-key stripping in `_subprocess_env()` is what makes
    subscription auth work, and a second code path would drift from it.
    """

    provider = "claude-cli"

    def __init__(self, *, model: str = DEFAULT_CLI_MODEL, binary: str = "claude",
                 timeout: float = DEFAULT_CLI_TIMEOUT, cwd: "str | None" = None,
                 runner=None, hardened: bool = True,
                 output_format: "str | None" = None) -> None:
        if output_format not in (None, "json"):
            # Not style policing. `--output-format stream-json` requires --verbose and
            # emits a newline-delimited event stream, not one envelope; accepting it would
            # return that stream verbatim as if it were the model's reply.
            raise ValueError(
                f"unsupported output_format {output_format!r}; use None (text) or 'json'")
        self.model = model
        self._binary = binary
        self._timeout = timeout
        self._cwd = cwd
        self._runner = runner
        self._hardened = hardened
        self._output_format = output_format
        # Provider usage + cost from the LAST SUCCESSFUL json-mode call. None before the
        # first call, None for every text-mode call, and None again after any failure --
        # see complete(). Public because it is a read-back channel, not state.
        self.last_usage: "dict | None" = None

    def _ensure_cwd(self) -> str:
        # A neutral, empty dir so `claude -p` doesn't pick up the repo's CLAUDE.md as
        # context. Shared PROCESS-WIDE via _process_scratch_cwd (see its docstring)
        # unless a caller passed an explicit `cwd=` to __init__, which is still
        # honoured verbatim -- tests that want an isolated or inspectable directory
        # keep working unchanged.
        if self._cwd is None:
            self._cwd = _process_scratch_cwd()
        return self._cwd

    def complete(self, system_prompt: str, user_prompt: str, *,
                 schema: "dict | None" = None) -> str:
        # Cleared on EVERY call, before anything can fail. A caller reading last_usage
        # after a failed call must not be handed the previous call's tokens and cost as
        # though they were this one's -- that is how one query silently reports another
        # query's spend on the disclosure surface.
        self.last_usage = None
        system = system_prompt
        if schema is not None:
            system = (f"{system_prompt}\n\nReply with ONLY a JSON value conforming to this JSON "
                      f"schema — no prose, no markdown fences:\n{json.dumps(schema)}")
        args = [self._binary, "-p", "--model", self.model,
                "--system-prompt", system, "--exclude-dynamic-system-prompt-sections"]
        if self._output_format is not None:
            # Appended BEFORE the hardening block so the hardening flags stay the trailing
            # group and the tests' suffix assertion keeps holding. Position is not otherwise
            # load-bearing: every assertion about this flag is index-based.
            args += ["--output-format", self._output_format]
        if self._hardened:
            # `claude -p` runs with LIVE filesystem tools unless told otherwise.
            # Verified in a temp cwd containing a canary file: without
            # `--tools ""` print mode really does execute Read on a relative path,
            # unprompted. _ensure_cwd's neutral directory constrains relative paths
            # only. A drone that reads fenced, untrusted note text must not hold tools.
            #
            # --strict-mcp-config + an empty --mcp-config also stops the subprocess
            # acquiring KB_MCP itself, which would be a circular capability and an
            # injection amplifier — that surface still exposes delete_memory.
            #
            # Measured cost of these flags: none within spread.
            args += ["--tools", "",
                     "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                     "--setting-sources", ""]
        try:
            proc = self._run(args, user_prompt)
        except FileNotFoundError as exc:
            raise InferenceError(
                f"claude CLI not found ('{self._binary}'); is Claude Code installed and on PATH?"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise InferenceError(f"claude CLI timed out after {self._timeout}s") from exc
        except InferenceError:
            raise
        except Exception as exc:
            raise InferenceError(f"claude CLI call failed: {exc}") from exc
        if self._output_format == "json":
            return self._json_envelope_reply(proc, schema=schema)
        if proc.returncode != 0:
            # `claude -p` (text mode) writes its error to STDOUT and exits non-zero, leaving
            # stderr empty — so surface both streams, else the error reads 'exited 1:' with no
            # cause (e.g. a usage-limit hit mid-crawl, a 4xx api_error).
            raise self._failure_error(proc.returncode, self._stream_detail(proc))
        out = (proc.stdout or "").strip()
        # Despite the "no markdown fences" instruction, the CLI model intermittently wraps
        # schema replies in ```json fences, which every downstream json.loads rejects (the
        # cause of sporadic labeler/curation parse failures). Unwrap on schema calls only,
        # so prose completions are never mangled.
        return _strip_fences(out) if schema is not None else out

    @staticmethod
    def _stream_detail(proc) -> str:
        """stdout + stderr, joined: the raw failure text, whatever the output mode.

        Shared by both modes rather than written twice, because the message they produce is
        parsed downstream. The quota failures the CLI reports read
        `claude CLI exited 1: You've hit your session limit · resets <time>`, and a
        KB_Retrieval breaker matches that text; two copies of this join would eventually
        disagree and only one of them would still be matched.
        """
        return " ".join(s.strip() for s in (proc.stdout, proc.stderr) if s and s.strip())

    def _failure_error(self, returncode, detail: str) -> InferenceError:
        """The error for a failed CLI call. ProviderUnavailable when `detail` matches
        a transient-outage shape (quota window, auth) so the enrichment worker defers
        the node; plain InferenceError otherwise. One message grammar either way —
        downstream matchers must not care which output mode or class produced it."""
        message = f"claude CLI exited {returncode}: {detail[:500]}"
        lowered = detail.lower()
        if any(marker in lowered for marker in _TRANSIENT_MARKERS):
            return ProviderUnavailable(message, provider=self.provider,
                                       until=_parse_reset(detail))
        return InferenceError(message)

    def _json_envelope_reply(self, proc, *, schema) -> str:
        """Decode one `claude -p --output-format json` result envelope -> the reply text.

        Verified against the real CLI with every hardening flag applied: exit 0, and an envelope
        shaped

            {"is_error": false, "duration_api_ms": 1386, "num_turns": 1,
             "stop_reason": "end_turn", "total_cost_usd": 0.000603, "usage": {...},
             "subtype": "success", "result": "ok", "type": "result"}

        FENCES -- `_strip_fences` is applied to `env["result"]` on schema calls and to
        NOTHING else. The two strings have different authors. The envelope is written by the
        CLI and is never fenced, so unwrapping it could only ever mask a real transport
        fault. `result` is the MODEL's own text and still carries the intermittent ```json
        wrapper the text path already unwraps (see complete()). Same rule, same condition
        (`schema is not None`), so a json-mode reply is byte-identical to the text-mode
        reply for the same model output.

        ERRORS -- both failure shapes raise InferenceError with the SAME message grammar the
        text path uses, `claude CLI exited {rc}: {detail}`, because the downstream quota
        breaker must not have to know which output mode produced the string:

          * non-zero exit: `detail` is the envelope's own `result` when the body parsed,
            else the raw stdout+stderr join. Preferring `result` is not cosmetic -- the raw
            body is a ~700-byte JSON blob whose `result` key sits AFTER `usage` and
            `modelUsage`, so the 500-char truncation below can cut the quota text out of it
            entirely.
          * exit 0 with `is_error: true`: the same grammar, honestly reporting rc 0. An
            envelope-only error is never swallowed as a successful reply -- that would hand
            the caller the string "You've hit your session limit ..." as if the model had
            said it, and the breaker would never fire.

        UNVERIFIED, deliberately: which of those two the CLI actually does on a session
        limit in json mode. Every observed occurrence so far was text mode (exit 1). Both
        are handled rather than guessed at; do not delete either branch on the strength of
        one observation.

        `last_usage` is set ONLY on the success path: `env["usage"]` plus `cost_usd` from
        `total_cost_usd`. `usage` is NOT a flat scalar map -- it nests `server_tool_use` and
        `cache_creation` dicts and an `iterations` LIST -- so a consumer that wants a single
        "provider tokens" number must SUM input_tokens + output_tokens +
        cache_read_input_tokens + cache_creation_input_tokens. Never ship the blob as a
        number.
        """
        raw = (proc.stdout or "").strip()
        env = None
        if raw:
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = None          # never let a bare JSONDecodeError escape this class
            if isinstance(decoded, dict):
                env = decoded
        result = env.get("result") if env is not None else None
        if proc.returncode != 0 or (env is not None and env.get("is_error")):
            detail = (result if isinstance(result, str) and result
                      else self._stream_detail(proc))
            raise self._failure_error(proc.returncode, detail)
        if env is None:
            raise InferenceError("claude CLI --output-format json returned unparseable "
                                 f"output: {raw[:500]!r}")
        if not isinstance(result, str):
            raise InferenceError("claude CLI --output-format json envelope has no string "
                                 f"'result' (keys: {sorted(env)[:12]})")
        usage = env.get("usage")
        self.last_usage = {**(usage if isinstance(usage, dict) else {}),
                           "cost_usd": env.get("total_cost_usd")}
        # .strip() on both branches: text mode strips stdout, and here the model's text is
        # `result` rather than stdout, so stripping it is what keeps the two modes equal.
        return _strip_fences(result) if schema is not None else result.strip()

    @staticmethod
    def _subprocess_env() -> dict:
        env = dict(os.environ)
        for var in _API_KEY_VARS:
            env.pop(var, None)
        return env

    def _run(self, args, stdin):
        env = self._subprocess_env()
        if self._runner is not None:
            return self._runner(args, stdin, self._timeout, self._ensure_cwd(), env)
        return subprocess.run(args, input=stdin, capture_output=True, text=True,
                              timeout=self._timeout, cwd=self._ensure_cwd(), env=env)
