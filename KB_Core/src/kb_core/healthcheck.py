"""kb-healthcheck: periodic KB health probe + alerting (observability).

The daemons only `print` to per-service logs, so a broken one is indistinguishable from an
idle one — which is how a provider auth failure can silently degrade enrichment and
concept labeling indefinitely. This probe closes that gap: it queries the danger signals,
writes a status file, and alerts (macOS notification when available; Pushover if
configured). Run it periodically (see the launchd agent).

Two severity tiers. PROBLEMS are failures: the probe exits non-zero so a supervisor/cron
notices, and notifies on every run while the condition persists. WARNINGS are signals that
are NOT failures and that persist once they appear (currently one producer: a capacity
warning when the O(n^2) memory_knn_edges build gets slow) — they never change the exit
code, and notify only on the run where they first appear, so a permanent condition doesn't
re-alert every 15 minutes and drown the notifications that mean something is actually
broken.

Signals: nodes stuck at enrichment_status='failed', enrichment failures in the last hour,
concept consolidation gone stale, a surge of placeholder concept names (labeling degraded),
enrichment tasks stuck 'processing', the concept kNN edge build slowing down (a capacity
warning, escalating to a problem), the inference server's health, daemon liveness, each
tenant's job queue / enrichment backlog / agent liveness, and oversized logs. `--rotate`
safely rotates every log the check warns about (rename + kickstart the
owning daemon so launchd reopens a fresh file — never truncate-in-place, which corrupts a
log the daemon still holds open).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from kb_core.claude_auth import claude_auth_status, claude_cli_in_use

# Launchd label prefix — must match kb-dev.sh's label()/kb-svc.sh's AGENT so this
# probe checks the same services those scripts actually installed.
_LABEL_PREFIX = os.environ.get("KB_SERVICE_PREFIX", "com.kb")

# Supervisor kb-run exports to its children: 'launchd' means daemons are
# launchd-managed, so this probe may query/kickstart them via launchctl; 'none' means
# kb-run itself owns restarts, and this module must make no launchctl calls at all.
# Default mirrors kb-run's own default: launchd on darwin (historic behavior), none
# elsewhere (there is no launchd off-macOS).
_SUPERVISOR = os.environ.get("KB_SUPERVISOR") or ("launchd" if sys.platform == "darwin"
                                                   else "none")

LOG_DIR = Path.home() / "Library" / "Logs" / "kb"
STATUS_FILE = LOG_DIR / "healthcheck-status.json"
LOG_WARN_BYTES = 50 * 1024 * 1024        # warn above 50 MB
# One number, deliberately: _log_signals warns ABOVE the warn threshold but rotates only
# ABOVE the rotate cap, so any gap between them is a band where the check nags every run
# with "run `kb-healthcheck --rotate`" — advice that cannot act on the file it names. Tie
# them, don't re-split them.
LOG_ROTATE_BYTES = LOG_WARN_BYTES        # rotate (with --rotate) above the same size
# memory_knn_edges is O(n^2) — every eligible memory probes every other, and its LATERAL
# scans a CTE, so HNSW is structurally unreachable there. At a few hundred vectors the
# sequential scan is fast and a forced-index variant was slower, which is why the
# rewrite is deferred rather than done. 5s is near the extrapolated crossover (low
# thousands of notes) where it starts paying; 60s means consolidation is already degraded.
KNN_WARN_MS = 5_000
KNN_PROBLEM_MS = 60_000
# Warning keys that are recorded (status file, printed output) but never notified, neither
# macOS nor Pushover. A tenant's offline agent is the tenant's news -- their own machine,
# shown on their dashboard -- not something to page the operator about.
_SILENT_WARNING_PREFIXES = ("tenant-agent-offline:",)
ALWAYS_ON = ("backend", "frontend", "inference", "ingest", "reanalyze", "resolve")
SERVICES = (*ALWAYS_ON, "consolidate")
# Per enabled tenant, kb-dev supervises ${prefix}.t.<slug>.<name> for each of these (kb-dev.sh
# TENANT_SERVICES), logging to t.<slug>.<name>.log. Same liveness rule as the owner's.
TENANT_SERVICES = ("ingest", "reanalyze", "resolve", "consolidate")
TENANT_ALWAYS_ON = ("ingest", "reanalyze", "resolve")
_TENANT_LOG_RE = re.compile(r"^t\.([a-z][a-z0-9_]{1,30})\.(" + "|".join(TENANT_SERVICES) + r")$")


def _dsn(env: "dict | None" = None) -> str:
    """KB_DATABASE_URL, else the same chain kb-svc.sh builds: the app role
    (KB_APP_USER/KB_APP_PASSWORD, falling back to POSTGRES_*), never a literal. This used
    to end in the bootstrap superuser `kb` with the password `kb`; an unset credential
    now fails loudly instead of trying a guessable default."""
    env = os.environ if env is None else env
    url = env.get("KB_DATABASE_URL")
    if url:
        return url
    user = env.get("KB_APP_USER") or env.get("POSTGRES_USER")
    password = env.get("KB_APP_PASSWORD") or env.get("POSTGRES_PASSWORD")
    if not user or not password:
        raise RuntimeError("no database credentials: set KB_DATABASE_URL, or "
                           "KB_APP_USER + KB_APP_PASSWORD (KB_Core/.env)")
    from psycopg.conninfo import make_conninfo
    return make_conninfo("", host="localhost", port=env.get("DB_HOST_PORT") or "5433",
                         dbname=env.get("POSTGRES_DB") or "kb_mcp", user=user,
                         password=password)


def _db_signals(problems: list, warnings: list, info: dict,
                dsn: "str | None" = None) -> None:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn or _dsn(), connect_timeout=5) as c:
        def one(q: str):
            with c.cursor() as cur:
                cur.execute(q)
                return cur.fetchone()[0]
        info["enrichment_failed"] = failed = one(
            "SELECT count(*) FROM nodes WHERE enrichment_status='failed'")
        if failed:
            problems.append(f"{failed} node(s) stuck at enrichment_status='failed' "
                            f"(re-queue via POST /api/ingest/retry-failed)")
        info["enrichment_failed_last_1h"] = recent = one(
            "SELECT count(*) FROM ingest_log WHERE event='enrichment_failed' "
            "AND at > now() - interval '1 hour'")
        if recent:
            problems.append(f"{recent} enrichment failure(s) in the last hour — the LLM "
                            f"provider may be down (check claude-cli / CLAUDE_CODE_OAUTH_TOKEN)")
        if one("SELECT coalesce(max(finished_at) < now() - interval '2 days', false) "
               "FROM concept_runs WHERE status='done'"):
            problems.append("concept consolidation has not completed in >2 days "
                            "(the consolidate daemon may be stuck)")
        info["active_concepts"] = active = one(
            "SELECT count(*) FROM concepts WHERE status='active'")
        info["placeholder_concepts"] = ph = one(
            "SELECT count(*) FROM concepts WHERE status='active' AND name LIKE '% / %'")
        if ph > 2:
            problems.append(f"{ph}/{active} active concepts have placeholder names — concept "
                            f"labeling may be degraded (claude-cli auth?)")
        # updated_at, not queued_at: a re-queued pile keeps its original queued_at
        # (re-driven nodes queued days earlier once false-fired this while actively
        # draining). updated_at stops moving exactly when a daemon dies
        # mid-batch, which is the condition this alarm exists for.
        info["stale_processing"] = stuck = one(
            "SELECT count(*) FROM node_processing WHERE task='enrichment' "
            "AND status='processing' AND updated_at < now() - interval '30 minutes'")
        if stuck:
            problems.append(f"{stuck} enrichment task(s) stuck 'processing' >30 min "
                            f"(a daemon likely crashed mid-batch)")

        # An active provider pause is the self-healing path WORKING (deferred work,
        # timed re-probe) — warn so it's visible, don't alarm.
        with c.cursor() as cur:
            cur.execute("SELECT provider, until, reason FROM provider_pause "
                        "WHERE until > now() ORDER BY provider")
            pauses = cur.fetchall()
        info["provider_pauses"] = [
            {"provider": p, "until": u.isoformat(), "reason": r} for p, u, r in pauses]
        for p, u, r in pauses:
            warnings.append((f"provider_paused_{p}",
                             f"provider {p} paused until {u:%Y-%m-%d %H:%M %Z} ({r}) — "
                             f"enrichment deferring; resumes automatically"))

        # --- consolidation kNN capacity ------------------------------------------
        # ORDER BY id, not started_at: id is a monotonic identity column, so it is the
        # unambiguous insertion order regardless of clock resolution. (started_at ties are
        # a test-seeding concern — the _seed_runs helper writes several rows in one
        # transaction, which share now(); in production each begin_concept_run is its own
        # transaction and started_at never ties.) jsonb_typeof pins the value to a number
        # so the ::float8 cast cannot raise on a malformed stats blob, and a row with no
        # knn_ms (every row predating this change) is skipped rather than treated as 0 —
        # a capacity alarm that really means "old schema" is worse than no alarm.
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT (stats->>'knn_ms')::float8 AS ms FROM concept_runs "
                        "WHERE status = 'done' "
                        "AND jsonb_typeof(stats->'knn_ms') = 'number' "
                        "ORDER BY id DESC LIMIT 3")
            recent = [r["ms"] for r in cur.fetchall()]
        if recent:
            # MEDIAN, not the latest: one run can be slow because the machine was busy,
            # and the notification fires on the transition and cannot be un-sent.
            knn_ms = statistics.median(recent)
            info["knn_ms"] = round(knn_ms, 1)
            # Same predicate as memory_knn_edges' `mems` CTE, deliberately duplicated:
            # this is a display number, so drift costs a wrong count in one message, never
            # a wrong verdict.
            info["knn_vectors"] = vectors = one(
                "SELECT count(*) FROM nodes WHERE embedding IS NOT NULL "
                "AND dead_at IS NULL AND entity_eligible "
                "AND type NOT IN ('folder', 'entity') "
                "AND NOT EXISTS (SELECT 1 FROM nodes a WHERE a.analysis_excluded "
                "AND nodes.path <@ a.path)")
            if knn_ms >= KNN_WARN_MS:
                msg = (f"concept kNN edge build is taking {knn_ms / 1000:.1f}s "
                       f"({vectors} eligible vectors) — memory_knn_edges is O(n^2); the "
                       f"incremental/HNSW rewrite is now due")
                if knn_ms >= KNN_PROBLEM_MS:
                    problems.append(msg)
                else:
                    # (key, message): the message embeds knn_ms/vectors, both volatile (they
                    # move on any ingest), so main()'s notify-once diff must key off the
                    # stable "knn_slow" identity rather than the rendered text — otherwise
                    # the diff sees a "new" warning on every run and re-notifies forever.
                    warnings.append(("knn_slow", msg))


def _inference_signal(problems: list, info: dict) -> None:
    url = os.environ.get("KB_INFERENCE_URL", "http://127.0.0.1:8001")
    try:
        from kb_core.ingest.inference import inference_auth_headers
        req = urllib.request.Request(f"{url}/health", headers=inference_auth_headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status == 200:
                info["inference"] = "ok"
            else:
                info["inference"] = f"status {r.status}"
                problems.append(f"inference server unhealthy (HTTP {r.status})")
    except Exception as exc:
        info["inference"] = f"error: {exc}"
        problems.append(f"inference server unreachable at {url}: {exc}")


def _daemon_signals(problems: list, info: dict, *, registry=None) -> None:
    if _SUPERVISOR != "launchd":
        return
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception as exc:
        info["daemons"] = f"launchctl error: {exc}"
        return
    # `launchctl list` rows: PID (or "-"), last exit status, label. Keyed by the exact label.
    rows: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows[parts[-1]] = (parts[0], parts[1])
    running = {s: rows[f"{_LABEL_PREFIX}.{s}"][0] for s in SERVICES
               if f"{_LABEL_PREFIX}.{s}" in rows}
    info["daemons"] = running
    for s in SERVICES:
        _daemon_row(problems, f"{_LABEL_PREFIX}.{s}", rows.get(f"{_LABEL_PREFIX}.{s}"),
                    always_on=s in ALWAYS_ON, owner=True)
    # Tenant daemons: a crash-looping com.kb.t.<slug>.* agent -- the
    # post-delete loop, a PolicyError -- was invisible here. Enabled tenants only: a
    # disabled tenant's daemons are expected to be stopped.
    try:
        from kb_core.tenants import load_registry
        reg = registry or load_registry()
        tenants = reg.tenants(include_owner=False)
    except Exception as exc:
        problems.append(f"tenant registry unreadable: {exc}")
        return
    per_tenant: dict = {}
    for t in tenants:
        seen: dict = {}
        for s in TENANT_SERVICES:
            label = f"{_LABEL_PREFIX}.t.{t.slug}.{s}"
            row = rows.get(label)
            if row is not None:
                seen[s] = row[0]
            _daemon_row(problems, label, row, always_on=s in TENANT_ALWAYS_ON, owner=False)
        per_tenant[t.slug] = seen
    info["tenant_daemons"] = per_tenant


def _daemon_row(problems: list, label: str, row, *, always_on: bool, owner: bool) -> None:
    """One label's liveness. The owner's wording is unchanged; a tenant's adds the last exit
    status, which is what tells a KeepAlive crash loop from a clean stop."""
    if row is None:
        problems.append(f"daemon {label} is not loaded")
    elif row[0] == "-" and always_on:
        if owner or row[1] in ("0", "-"):
            problems.append(f"daemon {label} has no PID (not running)")
        else:
            problems.append(f"daemon {label} has no PID (not running, last exit {row[1]})")


def _log_label(log: Path) -> str | None:
    """The launchd label that writes `log`: an owner service (<svc>.log) or a tenant daemon
    (t.<slug>.<svc>.log). None for a log no service owns."""
    stem = log.stem
    if stem in SERVICES:
        return f"{_LABEL_PREFIX}.{stem}"
    m = _TENANT_LOG_RE.match(stem)
    return f"{_LABEL_PREFIX}.t.{m.group(1)}.{m.group(2)}" if m else None


def _rotate_log(log: Path, problems: list, info: dict) -> None:
    # Safe rotation for a launchd-redirected log: rename (the running process keeps writing
    # to the renamed inode), then kickstart the owning daemon so launchd reopens a fresh path.
    label = _log_label(log)
    if label is None:
        return
    try:
        log.rename(log.with_suffix(".log.1"))
        if _SUPERVISOR == "launchd":
            subprocess.run(["launchctl", "kickstart", "-k",
                            f"gui/{os.getuid()}/{label}"], timeout=15)
        info.setdefault("rotated", []).append(log.name)
    except Exception as exc:
        problems.append(f"failed to rotate {log.name}: {exc}")


def _log_signals(problems: list, info: dict, rotate: bool) -> None:
    sizes: dict = {}
    for log in sorted(LOG_DIR.glob("*.log")):
        sz = log.stat().st_size
        sizes[log.name] = sz // (1024 * 1024)
        owned = _log_label(log) is not None
        if sz > LOG_ROTATE_BYTES and rotate and owned:
            _rotate_log(log, problems, info)
        elif sz > LOG_WARN_BYTES:
            # Never silent: a log --rotate cannot rotate (no service owns it) still warns.
            fix = ("run `kb-healthcheck --rotate`" if owned
                   else "no service owns it: rotate or remove it by hand")
            problems.append(f"log {log.name} is {sz // (1024 * 1024)} MB and unrotated ({fix})")
    info["log_sizes_mb"] = sizes


def _claude_auth_signal(problems: list, warnings: list, info: dict) -> None:
    """Credential health for the claude-cli provider.

    Severity split matches this module's two tiers. `out`/`critical` are PROBLEMS so
    they notify on every run while they persist. The 7-day notice is a WARNING, which
    notifies only on the run where its KEY first appears -- which is why the key must
    stay `claude_auth_expiring` with NO day count in it. A key like
    `claude_auth_expiring_6d` would be a fresh key every day and re-notify forever;
    see the kNN warning comment in main() for the same bug.
    """
    if not claude_cli_in_use():
        info["claude_auth"] = {"level": "unused", "days_left": None}
        return                    # no task routes to claude-cli: its login strands nothing
    st = claude_auth_status()
    info["claude_auth"] = {"level": st["level"], "days_left": st["days_left"]}
    if st["level"] == "out":
        problems.append("claude auth is signed out — enrichment will strand until "
                        "`claude auth login`")
    elif st["level"] == "critical":
        problems.append(f"claude auth expires in {st['days_left']:.1f} days "
                        f"({st['expires_at']}) — run `claude auth login`")
    elif st["level"] == "warn":
        warnings.append(("claude_auth_expiring",
                         f"claude auth expires in {st['days_left']:.1f} days "
                         f"({st['expires_at']}) — run `claude auth login`"))


def _error_line(exc: Exception, dsn: str) -> str:
    """First line of a driver error, with the DSN and its password scrubbed: tenant errors
    land in the status file and on stdout, which must never carry credentials."""
    msg = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
    msg = msg.replace(dsn, "<dsn>")
    password = urllib.parse.urlsplit(dsn).password
    return msg.replace(password, "***") if password else msg


def _tenant_signals(problems: list, warnings: list, info: dict, *, registry=None) -> None:
    """Per-tenant backlog and agent liveness. An offline agent is the TENANT's news (their
    dashboard shows it), so it is a WARNING after a week that never notifies at all (see
    _SILENT_WARNING_PREFIXES) -- it only shows in the status file and printed output. A
    tenant database we cannot reach, or can reach but not query (e.g. unmigrated), is ours:
    a problem, worded differently so the two are told apart. Error text is the first line
    of the driver's message with the DSN and password scrubbed. The owner is not listed
    here; _db_signals covers it."""
    from datetime import datetime, timedelta, timezone

    import psycopg
    from psycopg.rows import dict_row

    from kb_core.tenants import load_registry
    reg = registry or load_registry()
    out: dict = {}
    for t in reg.tenants(include_owner=False):
        dsn = reg.dsn_for(t)
        try:
            c = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
        except Exception as exc:
            error = f"database unreachable: {_error_line(exc, dsn)}"
        else:
            error = None
            try:
                with c:
                    jobs = {r["status"]: int(r["n"]) for r in c.execute(
                        "SELECT status, count(*) AS n FROM llm_jobs "
                        "WHERE status IN ('pending','claimed') GROUP BY status")}
                    seen = c.execute("SELECT last_seen_at FROM agent_state "
                                     "WHERE id = 1").fetchone()["last_seen_at"]
                    # Same breakdown as KnowledgeBase.enrichment_status_counts (the dashboard's).
                    enrichment = {r["enrichment_status"]: int(r["n"]) for r in c.execute(
                        "SELECT enrichment_status, count(*) AS n FROM nodes GROUP BY 1")}
                    paused = [r["provider"] for r in c.execute(
                        "SELECT provider FROM provider_pause WHERE until > now() "
                        "ORDER BY provider")]
                    # Pushed notes not yet drained. A dead tenant ingest daemon
                    # shows here as a growing number while jobs stay empty.
                    inbox_pending = int(c.execute(
                        "SELECT count(*) AS n FROM ingest_inbox "
                        "WHERE processed_at IS NULL").fetchone()["n"])
            except Exception as exc:
                error = f"query failed: {_error_line(exc, dsn)}"
        if error is not None:
            out[t.slug] = {"error": error}
            problems.append(f"tenant {t.slug}: {error}")
            continue
        out[t.slug] = {"jobs": jobs, "agent_last_seen_at": seen.isoformat() if seen else None,
                       "enrichment": enrichment, "paused": paused,
                       "inbox_pending": inbox_pending}
        if seen is not None and datetime.now(timezone.utc) - seen > timedelta(days=7):
            warnings.append((f"tenant-agent-offline:{t.slug}",
                             f"tenant {t.slug}: agent last seen {seen:%Y-%m-%d} "
                             f"(their machine, their call)"))
    info["tenants"] = out


def _notify(title: str, message: str) -> None:
    # ensure_ascii=False is REQUIRED, not cosmetic. json.dumps defaults to escaping every
    # non-ASCII character as \uXXXX, and AppleScript has no \u escape — it fails the whole
    # script with `syntax error (-2741)`. osascript still exits 0 and subprocess.run has no
    # check=True, so the notification silently never displayed and nothing reported it.
    # Nearly every alert string here carries an em-dash, so this killed the macOS channel
    # for exactly the messages worth interrupting someone about. The escapes json still
    # emits (\" and \\) are the ones AppleScript understands, so quoting stays correct.
    if sys.platform == "darwin":
        try:
            subprocess.run(["osascript", "-e",
                            f"display notification {json.dumps(message, ensure_ascii=False)} "
                            f"with title {json.dumps(title, ensure_ascii=False)}"], timeout=10)
        except Exception:
            pass
    tok, usr = os.environ.get("KB_PUSHOVER_TOKEN"), os.environ.get("KB_PUSHOVER_USER")
    if tok and usr:
        try:
            data = urllib.parse.urlencode(
                {"token": tok, "user": usr, "title": title, "message": message}).encode()
            urllib.request.urlopen(urllib.request.Request(
                "https://api.pushover.net/1/messages.json", data=data), timeout=10)
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="KB health probe + alerting")
    ap.add_argument("--rotate", action="store_true",
                    help="rotate logs over 50MB (restarts the owning daemon)")
    ap.add_argument("--quiet", action="store_true", help="print only when there are problems")
    args = ap.parse_args()

    problems: list = []
    warnings: list = []           # (key, message) — key is the notify-once identity
    info: dict = {}
    for probe in (lambda: _db_signals(problems, warnings, info),
                  lambda: _inference_signal(problems, info),
                  lambda: _daemon_signals(problems, info),
                  lambda: _claude_auth_signal(problems, warnings, info),
                  lambda: _tenant_signals(problems, warnings, info),
                  lambda: _log_signals(problems, info, args.rotate)):
        try:
            probe()
        except Exception as exc:
            problems.append(f"healthcheck probe error: {exc}")

    warning_keys = [k for k, _ in warnings]
    warning_messages = [m for _, m in warnings]

    # Read the PREVIOUS warning KEYS before overwriting the file. A warning is persistent by
    # nature (a corpus does not shrink), so it notifies once on the transition rather than on
    # every scheduled run — otherwise it would drown the signals that mean something is
    # actually down. Diffing on the KEY rather than the rendered message matters: a message
    # like the kNN one embeds volatile numbers (duration, live vector count) that change on
    # any ingest, so a text diff would see a "new" warning on every run and never stop
    # notifying. An absent or corrupt previous file is treated as empty, which fails toward
    # telling the owner.
    try:
        prev_warning_keys = set(json.loads(STATUS_FILE.read_text()).get("warning_keys", []))
    except Exception:
        prev_warning_keys = set()

    status = {"verdict": ("problems" if problems else "warnings" if warnings else "ok"),
              "problems": problems, "warnings": warning_messages,
              "warning_keys": warning_keys, "info": info}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(status, indent=2, default=str))
    except Exception:
        pass

    # Printed even under --quiet: a warning is news, the "ok" line is not.
    if warnings:
        print(f"[kb-healthcheck] WARNINGS ({len(warnings)}):", flush=True)
        for m in warning_messages:
            print(f"  - {m}", flush=True)
        fresh_keys = set(warning_keys) - prev_warning_keys
        fresh = [m for k, m in warnings
                 if k in fresh_keys and not k.startswith(_SILENT_WARNING_PREFIXES)]
        if fresh:
            _notify("KB capacity", "; ".join(fresh))

    # A run can produce both a fresh warning and a problem in the same pass (e.g. a capacity
    # crossing lands on the same run a daemon happens to break). Notify for both, on purpose:
    # swallowing the capacity warning under an unrelated problem would hide it on the one run
    # it was actually news, and a real problem must never go quiet just because a warning
    # also fired.
    if problems:
        print(f"[kb-healthcheck] PROBLEMS ({len(problems)}):", flush=True)
        for p in problems:
            print(f"  - {p}", flush=True)
        head = "; ".join(problems[:3])
        if len(problems) > 3:
            head += f" (+{len(problems) - 3} more)"
        _notify("KB health: problems detected", head)
        sys.exit(1)
    if not args.quiet:
        print(f"[kb-healthcheck] ok — {json.dumps(info, default=str)}", flush=True)


if __name__ == "__main__":
    main()
