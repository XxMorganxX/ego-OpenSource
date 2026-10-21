import json
import os
import sys

import psycopg
import pytest

from kb_core import healthcheck
from kb_core.db import migrate_pool, open_pool
from kb_core.tenants import Registry, Tenant
from tests.conftest import TEST_DATABASE_URL, tenant_login

URL_B = os.environ.get("KB_TEST_DATABASE_URL_B",
                       TEST_DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_DATABASE_URL.rsplit("/", 1)[1] + "2")


def _registry(db_name: str, dsn: str, **extra: Tenant):
    owner = Tenant("owner", frozenset({"m@x"}), database=db_name)
    a = Tenant("a", frozenset({"a@x"}), database=db_name, agent_secret="s" * 64, **tenant_login())
    dead = Tenant("dead", frozenset({"d@x"}), database="kb_t_dead_nope", agent_secret="s" * 64, **tenant_login())
    return Registry(owner, {"a": a, "dead": dead, **extra}, env={"KB_DATABASE_URL": dsn})


def test_tenant_signals_report_counts_and_flag_unreachable_databases(kb):
    with kb._pool.connection() as c:
        c.execute("TRUNCATE llm_jobs")
        c.execute("INSERT INTO llm_jobs (task, system_prompt, user_prompt) VALUES ('t', 's', 'u')")
        c.execute("UPDATE agent_state SET last_seen_at = now() - interval '10 days'")
        c.execute("INSERT INTO provider_pause (provider, until, reason) "
                  "VALUES ('agent', now() + interval '1 hour', 'quota')")
    problems, warnings, info = [], [], {}
    healthcheck._tenant_signals(problems, warnings, info,
                                registry=_registry(TEST_DATABASE_URL.rsplit("/", 1)[1], TEST_DATABASE_URL))
    a = info["tenants"]["a"]
    assert a["jobs"] == {"pending": 1} and a["agent_last_seen_at"] is not None and "enrichment" in a
    assert a["paused"] == ["agent"]
    assert "error" in info["tenants"]["dead"] and any("tenant dead" in p for p in problems)
    assert any(k == "tenant-agent-offline:a" for k, _ in warnings)
    assert "owner" not in info["tenants"]          # the owner is _db_signals' job


def test_each_tenant_reads_its_own_database(kb):
    """A second real tenant database: its numbers are its own, and an agent that has never
    connected (last_seen_at NULL) is not 'offline'."""
    try:
        psycopg.connect(URL_B, connect_timeout=3).close()
    except Exception:
        pytest.skip("KB_TEST_DATABASE_URL_B unreachable")
    pool_b = open_pool(URL_B, max_size=1, min_size=1)
    try:
        migrate_pool(pool_b)
        with pool_b.connection() as c:
            c.execute("TRUNCATE llm_jobs")
            c.execute("TRUNCATE provider_pause")
            c.execute("UPDATE agent_state SET last_seen_at = NULL")
    finally:
        pool_b.close()
    with kb._pool.connection() as c:
        c.execute("TRUNCATE llm_jobs")
        c.execute("INSERT INTO llm_jobs (task, system_prompt, user_prompt, status) "
                  "VALUES ('t', 's', 'u', 'claimed'), ('t', 's', 'u', 'done')")
        c.execute("UPDATE agent_state SET last_seen_at = now()")
    b = Tenant("b", frozenset({"b@x"}), database=URL_B.rsplit("/", 1)[1], agent_secret="s" * 64, **tenant_login())
    problems, warnings, info = [], [], {}
    healthcheck._tenant_signals(problems, warnings, info,
                                registry=_registry(TEST_DATABASE_URL.rsplit("/", 1)[1],
                                                   TEST_DATABASE_URL, b=b))
    assert info["tenants"]["a"]["jobs"] == {"claimed": 1}       # done is history, not backlog
    tb = info["tenants"]["b"]
    assert tb["jobs"] == {} and tb["agent_last_seen_at"] is None and tb["paused"] == []
    assert not [k for k, _ in warnings if k.startswith("tenant-agent-offline:")]


def test_no_registry_file_means_no_tenant_section(monkeypatch, tmp_path):
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(tmp_path / "none.toml"))
    problems, warnings, info = [], [], {}
    healthcheck._tenant_signals(problems, warnings, info)
    assert info["tenants"] == {} and problems == []


def test_connect_failure_and_query_failure_are_worded_apart(kb):
    """A database that connects but cannot answer (here: a search_path holding none of the
    tables, as an unmigrated tenant database looks) is a query failure, not 'unreachable'."""
    try:
        psycopg.connect(URL_B, connect_timeout=3).close()
    except Exception:
        pytest.skip("KB_TEST_DATABASE_URL_B unreachable")
    b = Tenant("b", frozenset({"b@x"}), database=URL_B.rsplit("/", 1)[1], agent_secret="s" * 64, **tenant_login())
    dead = Tenant("dead", frozenset({"d@x"}), database="kb_t_dead_nope", agent_secret="s" * 64, **tenant_login())
    reg = Registry(Tenant("owner", frozenset({"m@x"}), database="unused"), {"b": b, "dead": dead},
                   env={"KB_DATABASE_URL": URL_B + "?options=-csearch_path%3Dkb_no_such_schema"})
    problems, warnings, info = [], [], {}
    healthcheck._tenant_signals(problems, warnings, info, registry=reg)
    err_b, err_dead = info["tenants"]["b"]["error"], info["tenants"]["dead"]["error"]
    assert err_b.startswith("query failed: ") and "does not exist" in err_b and "\n" not in err_b
    assert err_dead.startswith("database unreachable: ") and "\n" not in err_dead
    assert f"tenant b: {err_b}" in problems and f"tenant dead: {err_dead}" in problems
    for text in (err_b, err_dead, *problems):
        assert reg.dsn_for(b) not in text and reg.dsn_for(dead) not in text


def test_error_text_never_carries_the_dsn_or_password(monkeypatch):
    pw = "s3cretpwxyz" * 3                                  # the TENANT's own password
    t = Tenant("t", frozenset({"t@x"}), database="kb_t_t", agent_secret="s" * 64,
               db_role="kb_t_t", db_password=pw)
    reg = Registry(Tenant("owner", frozenset({"m@x"}), database="kb_mcp"), {"t": t},
                   env={"KB_DATABASE_URL": "postgresql://kb_app:ownerpw@localhost:5433/kb_mcp"})
    dsn = reg.dsn_for(t)

    def leaky_connect(conninfo, **kw):
        raise psycopg.OperationalError(f"could not connect with {conninfo} (pw {pw})\nmore")

    monkeypatch.setattr(psycopg, "connect", leaky_connect)
    problems, warnings, info = [], [], {}
    healthcheck._tenant_signals(problems, warnings, info, registry=reg)
    assert info["tenants"]["t"]["error"] == (
        "database unreachable: could not connect with <dsn> (pw ***)")
    assert dsn not in problems[0] and pw not in problems[0]


def test_offline_agent_warning_never_notifies_but_other_new_warnings_do(
        tmp_path, monkeypatch, capsys):
    notes: list = []
    monkeypatch.setattr(healthcheck, "LOG_DIR", tmp_path)
    monkeypatch.setattr(healthcheck, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(healthcheck, "_notify", lambda t, m: notes.append((t, m)))
    monkeypatch.setattr(healthcheck, "_db_signals",
                        lambda p, w, i, dsn=None: w.append(("knn_slow", "kNN is slow")))
    monkeypatch.setattr(healthcheck, "_inference_signal", lambda p, i: None)
    monkeypatch.setattr(healthcheck, "_daemon_signals", lambda p, i: None)
    monkeypatch.setattr(healthcheck, "_claude_auth_signal", lambda p, w, i: None)
    monkeypatch.setattr(healthcheck, "_log_signals", lambda p, i, r: None)
    monkeypatch.setattr(healthcheck, "_tenant_signals", lambda p, w, i: w.append(
        ("tenant-agent-offline:a", "tenant a: agent last seen 2026-09-01")))
    monkeypatch.setattr(sys, "argv", ["kb-healthcheck"])

    healthcheck.main()
    assert notes == [("KB capacity", "kNN is slow")]          # the offline agent is not in it
    status = json.loads((tmp_path / "status.json").read_text())
    assert "tenant-agent-offline:a" in status["warning_keys"]
    assert "tenant a: agent last seen 2026-09-01" in status["warnings"]
    assert "tenant a: agent last seen 2026-09-01" in capsys.readouterr().out

    # Alone and new (no previous status file): it still never reaches _notify.
    notes.clear()
    (tmp_path / "status.json").unlink()
    monkeypatch.setattr(healthcheck, "_db_signals", lambda p, w, i, dsn=None: None)
    healthcheck.main()
    assert notes == []


# ---- tenant daemons, inbox backlog and tenant logs are visible --------------------------

def _launchctl(lines: list[str]):
    out = "PID\tStatus\tLabel\n" + "\n".join(lines) + "\n"
    calls: list = []

    def run(cmd, *a, **k):
        calls.append(cmd)
        return type("R", (), {"stdout": out if cmd[:2] == ["launchctl", "list"] else ""})()
    return run, calls


def test_daemon_signals_cover_every_enabled_tenants_daemons(monkeypatch):
    p = healthcheck._LABEL_PREFIX
    owner = [f"{100 + i}\t0\t{p}.{s}" for i, s in enumerate(healthcheck.SERVICES)]
    run, _ = _launchctl(owner + [
        f"456\t0\t{p}.t.a.ingest",
        f"-\t1\t{p}.t.a.reanalyze",          # KeepAlive crash loop: no PID, last exit 1
        f"789\t0\t{p}.t.a.resolve",           # consolidate: not loaded at all
    ])
    monkeypatch.setattr(healthcheck, "_SUPERVISOR", "launchd")
    monkeypatch.setattr(healthcheck.subprocess, "run", run)
    off = Tenant("off", frozenset({"o@x"}), database="kb_t_off", agent_secret="o" * 64, **tenant_login(),
                 enabled=False)
    reg = Registry(Tenant("owner", frozenset({"m@x"}), database="kb_mcp"),
                   {"a": Tenant("a", frozenset({"a@x"}), database="kb_t_a", agent_secret="s" * 64, **tenant_login()),
                    "off": off}, env={})
    problems, info = [], {}
    healthcheck._daemon_signals(problems, info, registry=reg)
    assert sorted(problems) == sorted([
        f"daemon {p}.t.a.reanalyze has no PID (not running, last exit 1)",
        f"daemon {p}.t.a.consolidate is not loaded",
    ])
    assert info["tenant_daemons"]["a"] == {"ingest": "456", "reanalyze": "-", "resolve": "789"}
    assert "off" not in info["tenant_daemons"]               # disabled: not expected to run
    assert set(info["daemons"]) == set(healthcheck.SERVICES)  # owner rows unchanged


def test_tenant_signals_report_inbox_pending_per_database(kb):
    """A dead tenant ingest daemon shows as a growing inbox, not as 'healthy'."""
    try:
        psycopg.connect(URL_B, connect_timeout=3).close()
    except Exception:
        pytest.skip("KB_TEST_DATABASE_URL_B unreachable")
    pool_b = open_pool(URL_B, max_size=1, min_size=1)
    try:
        migrate_pool(pool_b)
        with pool_b.connection() as c:
            c.execute("DELETE FROM ingest_inbox")
            c.execute("INSERT INTO ingest_inbox (source, external_id, event_type, payload, "
                      "processed_at) VALUES ('vault', 'x', 'upsert', '{}', now())")
    finally:
        pool_b.close()
    with kb._pool.connection() as c:
        c.execute("DELETE FROM ingest_inbox")
        c.execute("INSERT INTO ingest_inbox (source, external_id, event_type, payload) VALUES "
                  "('vault', 'p1', 'upsert', '{}'), ('vault', 'p2', 'upsert', '{}')")
    b = Tenant("b", frozenset({"b@x"}), database=URL_B.rsplit("/", 1)[1], agent_secret="b" * 64, **tenant_login())
    problems, warnings, info = [], [], {}
    healthcheck._tenant_signals(problems, warnings, info,
                                registry=_registry(TEST_DATABASE_URL.rsplit("/", 1)[1],
                                                   TEST_DATABASE_URL, b=b))
    assert info["tenants"]["a"]["inbox_pending"] == 2
    assert info["tenants"]["b"]["inbox_pending"] == 0        # processed rows are history


def test_rotate_covers_tenant_logs_and_warns_on_logs_nobody_owns(tmp_path, monkeypatch):
    p = healthcheck._LABEL_PREFIX
    monkeypatch.setattr(healthcheck, "LOG_DIR", tmp_path)
    monkeypatch.setattr(healthcheck, "_SUPERVISOR", "launchd")
    run, calls = _launchctl([])
    monkeypatch.setattr(healthcheck.subprocess, "run", run)
    big = healthcheck.LOG_WARN_BYTES + 1
    for name in ("t.alice.ingest.log", "mystery.log"):
        (tmp_path / name).write_bytes(b"")
        os.truncate(tmp_path / name, big)
    problems, info = [], {}
    healthcheck._log_signals(problems, info, rotate=True)
    assert info["rotated"] == ["t.alice.ingest.log"]
    assert (tmp_path / "t.alice.ingest.log.1").exists()
    assert ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{p}.t.alice.ingest"] in calls
    assert any("mystery.log" in m and "unrotated" in m for m in problems)   # never silent
    assert (tmp_path / "mystery.log").exists()
