"""kb-healthcheck's DB probe flags the silent-failure signals.

Regression guard: a provider outage stays hidden when nothing watches these signals.
"""
import json
import os
import sys

import pytest

from kb_core import healthcheck
from kb_core.domain import NodeRecord


def _test_dsn() -> str:
    return os.environ.get("KB_TEST_DATABASE_URL",
                          "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test")


def test_flags_stuck_failed_enrichment(kb):
    n = kb.upsert(NodeRecord(source="agent", external_id="f", type="note",
                             content="body", slug="f"))
    with kb._pool.connection() as c:
        c.execute("UPDATE nodes SET enrichment_status='failed' WHERE id=%s", (n.id,))
    problems, warnings, info = [], [], {}
    healthcheck._db_signals(problems, warnings, info, dsn=_test_dsn())
    assert info["enrichment_failed"] >= 1
    assert any("enrichment_status='failed'" in p for p in problems)


def test_clean_db_reports_no_problems(kb):
    problems, warnings, info = [], [], {}
    healthcheck._db_signals(problems, warnings, info, dsn=_test_dsn())
    assert info["enrichment_failed"] == 0
    assert not any("enrichment_status='failed'" in p for p in problems)
    assert info["placeholder_concepts"] == 0


def _isolate_main(monkeypatch, tmp_path, notes: list) -> None:
    """Point main() at tmp files and stub EVERY probe that reads the machine it runs on:
    inference, launchd daemons, `claude auth status`, the tenant registry, and the log dir.
    Each test then supplies its own _db_signals. Stubbing only some of them made these
    tests pass on a dev Mac (claude logged in, inference up) and fail on CI, where the
    unstubbed claude-auth probe found no `claude` binary and reported a problem."""
    monkeypatch.setattr(healthcheck, "LOG_DIR", tmp_path)
    monkeypatch.setattr(healthcheck, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(healthcheck, "_notify", lambda t, m: notes.append((t, m)))
    monkeypatch.setattr(healthcheck, "_inference_signal", lambda p, i: None)
    monkeypatch.setattr(healthcheck, "_daemon_signals", lambda p, i: None)
    monkeypatch.setattr(healthcheck, "_claude_auth_signal", lambda p, w, i: None)
    monkeypatch.setattr(healthcheck, "_tenant_signals", lambda p, w, i: None)
    monkeypatch.setattr(healthcheck, "_log_signals", lambda p, i, r: None)


def test_warning_does_not_change_exit_code_and_notifies_once(tmp_path, monkeypatch):
    """A capacity warning is not a failure: exit stays 0, and it notifies only on the
    transition. Otherwise every scheduled run would re-alert for a corpus that cannot
    shrink, training Avery to ignore the notifications that mean something is down."""
    notes: list = []
    _isolate_main(monkeypatch, tmp_path, notes)
    monkeypatch.setattr(healthcheck, "_db_signals",
                        lambda p, w, i, dsn=None: w.append(("knn_slow", "kNN is slow")))
    monkeypatch.setattr(sys, "argv", ["kb-healthcheck"])

    healthcheck.main()                    # returns normally -> exit code 0
    assert len(notes) == 1

    status = json.loads((tmp_path / "status.json").read_text())
    assert status["verdict"] == "warnings"
    assert status["warnings"] == ["kNN is slow"]
    assert status["warning_keys"] == ["knn_slow"]
    assert status["problems"] == []

    healthcheck.main()                    # same warning, already in the status file
    assert len(notes) == 1                # still 1 -- no re-notification


def test_warning_message_change_with_same_key_does_not_renotify(tmp_path, monkeypatch):
    """The regression this fix exists to prevent: the kNN message embeds volatile numbers
    (duration, live eligible-vector count) that change on any ingest, so the rendered text
    differs run to run even when the underlying condition (the key) has not. Diffing on
    message text would re-notify every 15 minutes forever; diffing on the stable key must
    not."""
    notes: list = []
    _isolate_main(monkeypatch, tmp_path, notes)
    monkeypatch.setattr(sys, "argv", ["kb-healthcheck"])

    messages = iter([
        "concept kNN edge build is taking 5.2s (120 eligible vectors)",
        "concept kNN edge build is taking 5.4s (128 eligible vectors)",   # vector count moved
    ])
    monkeypatch.setattr(
        healthcheck, "_db_signals",
        lambda p, w, i, dsn=None: w.append(("knn_slow", next(messages))))

    healthcheck.main()
    assert len(notes) == 1

    healthcheck.main()                    # message text changed, key did not
    assert len(notes) == 1                # still 1 -- no re-notification

    status = json.loads((tmp_path / "status.json").read_text())
    assert status["warnings"] == ["concept kNN edge build is taking 5.4s (128 eligible vectors)"]


def test_problem_still_exits_nonzero_with_warnings_present(tmp_path, monkeypatch):
    """Both tiers firing in the same run is intentional (see main()'s comment): a fresh
    capacity warning must not be swallowed by an unrelated problem, so both notifications
    fire, and the status file's warnings must still be right for a file reader."""
    notes: list = []
    _isolate_main(monkeypatch, tmp_path, notes)
    monkeypatch.setattr(
        healthcheck, "_db_signals",
        lambda p, w, i, dsn=None: (w.append(("knn_slow", "slow")), p.append("broken")))
    monkeypatch.setattr(sys, "argv", ["kb-healthcheck"])

    try:
        healthcheck.main()
        raised = None
    except SystemExit as exc:
        raised = exc.code
    assert raised == 1
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["verdict"] == "problems"
    assert status["warnings"] == ["slow"]                      # the reader needs this right

    titles = [t for t, _ in notes]
    assert "KB capacity" in titles and "KB health: problems detected" in titles


def _seed_runs(kb, *knn_ms_values, status="done"):
    """Insert concept_runs rows oldest-first. A value of None writes a stats blob with no
    knn_ms key — the pre-change schema every existing row has."""
    with kb._pool.connection() as c:
        for v in knn_ms_values:
            stats = "{}" if v is None else json.dumps({"knn_ms": v})
            c.execute("INSERT INTO concept_runs (params, status, stats, finished_at) "
                      "VALUES ('{}'::jsonb, %s, %s::jsonb, now())", (status, stats))


def _probe(kb):
    problems, warnings, info = [], [], {}
    healthcheck._db_signals(problems, warnings, info, dsn=_test_dsn())
    return problems, warnings, info


def test_fast_knn_build_is_silent(kb):
    _seed_runs(kb, 150.0, 162.0, 171.0)
    problems, warnings, info = _probe(kb)
    assert info["knn_ms"] == 162.0
    assert not any("kNN" in m for m in warnings + problems)


def test_slow_knn_build_warns_without_becoming_a_problem(kb):
    _seed_runs(kb, 6000.0, 7000.0, 8000.0)
    problems, warnings, info = _probe(kb)
    assert len(warnings) == 1
    key, msg = warnings[0]
    assert key == "knn_slow"
    assert "memory_knn_edges is O(n" in msg
    assert "7.0s" in msg
    assert not any("kNN" in p for p in problems)


def test_very_slow_knn_build_escalates_to_a_problem(kb):
    _seed_runs(kb, 61_000.0, 62_000.0, 63_000.0)
    problems, warnings, info = _probe(kb)
    assert any("memory_knn_edges is O(n" in p for p in problems)
    assert warnings == []


def test_runs_without_knn_ms_never_warn(kb):
    """Every concept_runs row predates this change. A missing key must stay silent --
    a capacity alarm that really means 'old schema' is the failure to avoid."""
    _seed_runs(kb, None, None, None)
    problems, warnings, info = _probe(kb)
    assert "knn_ms" not in info
    assert not any("kNN" in m for m in warnings + problems)


def test_a_single_slow_run_cannot_trip_the_warning(kb):
    """Median of 3, not the latest: a run slow because the machine was busy must not fire
    a notification that cannot be un-sent."""
    _seed_runs(kb, 200.0, 200.0, 90_000.0)
    problems, warnings, info = _probe(kb)
    assert info["knn_ms"] == 200.0
    assert warnings == [] and not any("kNN" in p for p in problems)


def test_failed_runs_are_ignored(kb):
    _seed_runs(kb, 90_000.0, status="failed")
    problems, warnings, info = _probe(kb)
    assert "knn_ms" not in info


def test_notify_does_not_emit_unicode_escapes_to_osascript(monkeypatch):
    """AppleScript has no \\u escape, so json.dumps' default ensure_ascii=True turns any
    non-ASCII character into a hard `syntax error (-2741)` and the notification silently
    never displays — osascript exits 0 and subprocess.run has no check=True, so nothing
    reports the failure. Most real alert text carries an em-dash, so this killed the
    macOS channel for exactly the messages that matter.

    _notify only shells osascript when sys.platform == "darwin" (KB_SUPERVISOR/platform
    portability) — pin the platform so this test exercises that path regardless
    of the runner OS; CI runs on ubuntu-latest, where the branch would otherwise be
    skipped and `calls` would stay empty."""
    monkeypatch.setattr(healthcheck.sys, "platform", "darwin")
    calls: list = []
    monkeypatch.setattr(healthcheck.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    monkeypatch.delenv("KB_PUSHOVER_TOKEN", raising=False)
    monkeypatch.delenv("KB_PUSHOVER_USER", raising=False)

    healthcheck._notify("KB capacity", 'kNN build 6.2s — O(n^2) "q" back\\slash')

    script = calls[0][2]
    assert "\\u" not in script                    # the bug: — breaks AppleScript
    assert "—" in script                          # the literal character survives
    assert '\\"q\\"' in script                    # quotes still escaped for AppleScript
    assert "back\\\\slash" in script              # backslashes still escaped


def _auth_signal(monkeypatch, level, days_left=None):
    from kb_core import healthcheck
    monkeypatch.setattr(healthcheck, "claude_auth_status",
                        lambda: {"level": level, "logged_in": level != "out",
                                 "days_left": days_left,
                                 "expires_at": "2026-09-07T19:32:44+00:00",
                                 "email": "m@example.com"})
    problems, warnings, info = [], [], {}
    healthcheck._claude_auth_signal(problems, warnings, info)
    return problems, warnings, info


def test_signed_out_is_a_problem(monkeypatch):
    problems, warnings, _ = _auth_signal(monkeypatch, "out")
    assert problems and not warnings          # problems notify every run


def test_critical_is_a_problem(monkeypatch):
    problems, warnings, _ = _auth_signal(monkeypatch, "critical", 1.2)
    assert problems and not warnings


def test_warn_is_a_warning_keyed_without_digits(monkeypatch):
    problems, warnings, _ = _auth_signal(monkeypatch, "warn", 6.4)
    assert not problems and len(warnings) == 1
    key, message = warnings[0]
    assert key == "claude_auth_expiring"
    # A key carrying the day count would mint a new key daily and re-notify forever
    # — the bug healthcheck's own kNN comment documents.
    assert not any(c.isdigit() for c in key)
    assert "6.4" in message                    # the number belongs in the message


def test_ok_is_silent(monkeypatch):
    problems, warnings, info = _auth_signal(monkeypatch, "ok", 20.0)
    assert not problems and not warnings
    assert info["claude_auth"]["level"] == "ok"


def test_provider_pause_is_a_warning_not_a_problem(kb):
    from datetime import datetime, timedelta, timezone
    kb.pause_provider("claude-cli",
                      until=datetime.now(timezone.utc) + timedelta(hours=2),
                      reason="session limit")
    problems, warnings, info = [], [], {}
    healthcheck._db_signals(problems, warnings, info, dsn=_test_dsn())
    assert not any("paused" in p for p in problems)
    assert any(key == "provider_paused_claude-cli" for key, _ in warnings)
    [p] = info["provider_pauses"]
    assert p["provider"] == "claude-cli" and "session limit" in p["reason"]


def test_stale_processing_keys_on_activity_not_queue_age(kb, pool):
    from kb_core.domain import NodeRecord
    n = kb.upsert(NodeRecord(source="agent", external_id="sp1", type="note",
                             content="c", slug="sp1", parent_path=""))
    # Re-driven pile shape (a past false alarm): queued days ago, touched now.
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO node_processing (node_id, task, status, queued_at, updated_at)
               VALUES (%s, 'enrichment', 'processing',
                       now() - interval '2 days', now())""", (n.id,))
    problems, warnings, info = [], [], {}
    healthcheck._db_signals(problems, warnings, info, dsn=_test_dsn())
    assert info["stale_processing"] == 0
    assert not any("stuck 'processing'" in p for p in problems)
    # The genuinely-stuck shape — updated_at ALSO old — must still fire.
    with pool.connection() as conn:
        conn.execute("UPDATE node_processing SET updated_at = now() - interval '2 days' "
                     "WHERE node_id = %s AND task = 'enrichment'", (n.id,))
    problems, warnings, info = [], [], {}
    healthcheck._db_signals(problems, warnings, info, dsn=_test_dsn())
    assert info["stale_processing"] == 1


def test_daemon_signals_skipped_without_launchd(monkeypatch):
    """_SUPERVISOR is read at import time from KB_SUPERVISOR, so proving 'none' is honored
    requires reloading the module after setting it -- same hazard as _LABEL_PREFIX (see
    test_label_prefix_env). The first reload must stay inside the try: if the assertion
    below it fails, the finally still restores the launchd-default module for every later
    test in the process."""
    monkeypatch.setenv("KB_SUPERVISOR", "none")
    import importlib

    from kb_core import healthcheck
    try:
        importlib.reload(healthcheck)
        problems, info = [], {}
        called = []
        monkeypatch.setattr(healthcheck.subprocess, "run",
                            lambda *a, **k: called.append(a) or type("R", (), {"stdout": ""})())
        healthcheck._daemon_signals(problems, info)
        assert called == [] and problems == []
    finally:
        monkeypatch.undo()
        importlib.reload(healthcheck)


def test_notify_no_osascript_off_macos(monkeypatch):
    from kb_core import healthcheck
    monkeypatch.setattr(healthcheck.sys, "platform", "linux")
    monkeypatch.delenv("KB_PUSHOVER_TOKEN", raising=False)
    monkeypatch.delenv("KB_PUSHOVER_USER", raising=False)
    called = []
    monkeypatch.setattr(healthcheck.subprocess, "run", lambda *a, **k: called.append(a))
    healthcheck._notify("t", "m")
    assert called == []


def test_label_prefix_env(monkeypatch):
    """_LABEL_PREFIX is read at import time from KB_SERVICE_PREFIX, so proving the env
    var is honored requires reloading the module after setting it — the same hazard as
    dashboard/api/app.py's _ALLOWED_ORIGINS (see test_dashboard_csrf.py). The first
    reload must stay inside the try: if the assertion below it fails, the `finally`
    still restores the un-prefixed module for every later test in the process."""
    monkeypatch.setenv("KB_SERVICE_PREFIX", "com.example.kb")
    import importlib

    from kb_core import healthcheck
    try:
        importlib.reload(healthcheck)
        assert healthcheck._LABEL_PREFIX == "com.example.kb"
    finally:
        monkeypatch.undo()
        importlib.reload(healthcheck)


def _sized_log(tmp_path, name: str, size: int):
    """A log file of exactly `size` bytes. Sparse (truncate, not write) — the thresholds
    are tens of MB and only st_size is read."""
    log = tmp_path / name
    log.write_bytes(b"")
    os.truncate(log, size)
    return log


def test_a_log_big_enough_to_warn_is_one_that_rotate_can_actually_rotate(tmp_path,
                                                                        monkeypatch):
    """The warn threshold and the rotate cap must be the same number.

    If they differ (say 50 MB and 100 MB), a log in between nags on every run with
    "run `kb-healthcheck --rotate`" — naming a remedy that cannot touch it, because
    _log_signals only rotates ABOVE the cap. Whatever the numbers become, a log
    the check complains about has to be a log the advice it prints actually rotates.
    """
    monkeypatch.setattr(healthcheck, "LOG_DIR", tmp_path)
    monkeypatch.setattr(healthcheck, "_SUPERVISOR", "none")   # no launchctl kickstart
    _sized_log(tmp_path, "ingest.log", healthcheck.LOG_WARN_BYTES + 1)

    problems, info = [], {}
    healthcheck._log_signals(problems, info, rotate=True)

    assert info.get("rotated") == ["ingest.log"]
    assert problems == []
    assert (tmp_path / "ingest.log.1").exists()


def test_an_oversized_log_still_warns_when_rotate_is_not_asked_for(tmp_path, monkeypatch):
    """Lowering the cap must not silence the plain (no --rotate) run: that warning is the
    only thing that tells a human the log is growing. It must also not rotate behind their
    back — a rename here would restart the owning daemon without being asked."""
    monkeypatch.setattr(healthcheck, "LOG_DIR", tmp_path)
    monkeypatch.setattr(healthcheck, "_SUPERVISOR", "none")
    _sized_log(tmp_path, "ingest.log", healthcheck.LOG_WARN_BYTES + 1)

    problems, info = [], {}
    healthcheck._log_signals(problems, info, rotate=False)

    assert any("ingest.log" in p and "unrotated" in p for p in problems)
    assert "rotated" not in info
    assert not (tmp_path / "ingest.log.1").exists()


def test_claude_auth_is_not_checked_when_nothing_routes_to_claude_cli(monkeypatch, tmp_path):
    from kb_core import healthcheck
    from kb_core.inference_routing import TASKS
    p = tmp_path / "inference.toml"
    p.write_text("".join(f'[task.{n}]\nprovider = "local"\n' for n in TASKS))
    monkeypatch.setenv("KB_INFERENCE_ROUTING", str(p))
    monkeypatch.setattr(healthcheck, "claude_auth_status",
                        lambda: pytest.fail("probed claude auth with no claude-cli task"))
    problems, warnings, info = [], [], {}
    healthcheck._claude_auth_signal(problems, warnings, info)
    assert not problems and not warnings
    assert info["claude_auth"]["level"] == "unused"


def test_dsn_never_falls_back_to_a_literal_superuser_password():
    with pytest.raises(RuntimeError, match="no database credentials"):
        healthcheck._dsn({})
    with pytest.raises(RuntimeError):
        healthcheck._dsn({"POSTGRES_USER": "kb"})                 # no password: refuse


def test_dsn_prefers_the_url_then_the_app_role_like_kb_svc():
    from psycopg.conninfo import conninfo_to_dict
    assert healthcheck._dsn({"KB_DATABASE_URL": "postgresql://a:b@h/d"}) == \
        "postgresql://a:b@h/d"
    info = conninfo_to_dict(healthcheck._dsn({
        "KB_APP_USER": "kb_app", "KB_APP_PASSWORD": "p@ss/word",
        "POSTGRES_USER": "kb", "POSTGRES_PASSWORD": "super", "DB_HOST_PORT": "5544"}))
    assert info == {"host": "localhost", "port": "5544", "dbname": "kb_mcp",
                    "user": "kb_app", "password": "p@ss/word"}
