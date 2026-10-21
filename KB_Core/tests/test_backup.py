import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from kb_core import backup as B
from kb_core.tenants import Registry, Tenant
from tests.conftest import tenant_login


class FakeRun:
    """Records argv; creates the output file pg_dump/age would have written."""
    def __init__(self):
        self.calls = []

    def __call__(self, argv, check=True, **kw):
        self.calls.append(argv)
        if argv[0].endswith("pg_dump"):
            Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP")
        if argv[0] == "age" and "--output" in argv:
            Path(argv[argv.index("--output") + 1]).write_bytes(b"age-encrypted")
        if argv[0].endswith("pg_restore") and "--list" in argv:
            return type("P", (), {"returncode": 0, "stdout": LIST_WITH_DATA, "stderr": b""})()
        return type("P", (), {"returncode": 0, "stdout": b"PGDMP"})()


LIST_WITH_DATA = b"240; 1259 722279000 TABLE public nodes kb_app\n4400; 0 722279500 TABLE DATA public nodes kb_app\n"


def _healthy(dsn):
    """post_restore stub: nothing to migrate, every check passes."""
    return [], []


def _registry():
    owner = Tenant("owner", frozenset({"m@x"}), database="kb_mcp")
    a = Tenant("a", frozenset({"a@x"}), database="kb_t_a", agent_secret="s" * 64, **tenant_login())
    off = Tenant("off", frozenset({"o@x"}), database="kb_t_off", agent_secret="s" * 64, **tenant_login(), enabled=False)
    return Registry(owner, {"a": a, "off": off}, env={"KB_APP_USER": "u", "KB_APP_PASSWORD": "p"})


def test_run_backups_dumps_every_database_including_disabled_and_encrypts(tmp_path):
    run = FakeRun()
    now = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)
    written = B.run_backups(_registry(), out_dir=tmp_path, recipient="age1xyz", now=now, run=run,
                            env={"KB_PG_BIN": "/pg/bin"})
    assert [p.relative_to(tmp_path).as_posix() for p in written] == [
        "kb_mcp/20261009T030000Z.dump.age", "kb_t_a/20261009T030000Z.dump.age", "kb_t_off/20261009T030000Z.dump.age"]
    assert all(p.read_bytes() == b"age-encrypted" and oct(p.stat().st_mode & 0o777) == "0o600" for p in written)
    dump_calls = [c for c in run.calls if c[0] == "/pg/bin/pg_dump"]
    assert dump_calls[0][:5] == ["/pg/bin/pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--dbname"]
    dsn_arg = dump_calls[1][dump_calls[1].index("--dbname") + 1]
    assert "dbname=kb_t_a" in dsn_arg and "password" not in dsn_arg      # password goes by env
    assert not list(tmp_path.glob("**/*.tmp"))                              # plaintext temp removed
    assert any(c[0] == "age" and c[1:3] == ["--recipient", "age1xyz"] for c in run.calls)


def test_apply_retention_keeps_14_daily_then_one_per_week_for_8_weeks(tmp_path):
    d = tmp_path / "kb_t_a"; d.mkdir()
    start = datetime(2026, 10, 9, tzinfo=timezone.utc)
    for i in range(120):
        (d / B.backup_name(start - timedelta(days=i))).write_bytes(b"x")
    removed = B.apply_retention(d, daily=14, weekly=8)
    kept = sorted(f.name for f in d.glob("*.dump.age"))
    assert len(kept) == 14 + 8 and len(removed) == 120 - 22
    assert kept[-1] == B.backup_name(start)                                 # newest always kept


def test_restore_pipes_the_decrypted_dump_into_pg_restore(tmp_path):
    run = FakeRun()
    f = tmp_path / "x.dump.age"; f.write_bytes(b"age")
    B.restore(f, into_dsn="postgresql://u:p@localhost:5433/kb_t_a", identity=tmp_path / "key.txt",
              run=run, env={"KB_PG_BIN": "/pg/bin"}, post_restore=_healthy)
    assert run.calls[0][:3] == ["age", "--decrypt", "--identity"]
    assert run.calls[-1][0] == "/pg/bin/pg_restore" and "--dbname" in run.calls[-1]


TOC = """;
; Archive created at 2026-10-09 12:00:00 UTC
;
2; 3079 722277577 EXTENSION - ltree
4216; 0 0 COMMENT - EXTENSION ltree
3; 3079 722277762 EXTENSION - vector
4218; 0 0 COMMENT - EXTENSION vector
240; 1259 722279000 TABLE public nodes kb_app
4400; 0 722279500 TABLE DATA public nodes kb_app
5000; 2620 722279100 TRIGGER public nodes nodes_notify_update kb_app
"""


class TocRun(FakeRun):
    """`pg_restore --list` prints TOC; the restoring call's --use-list file is captured."""
    def __init__(self):
        super().__init__()
        self.use_list = None

    def __call__(self, argv, check=True, **kw):
        self.calls.append(argv)
        if argv[0].endswith("pg_restore") and "--list" in argv:
            return type("P", (), {"returncode": 0, "stdout": TOC.encode()})()
        if argv[0].endswith("pg_restore") and "--use-list" in argv:
            self.use_list = Path(argv[argv.index("--use-list") + 1]).read_text()
        return type("P", (), {"returncode": 0, "stdout": b"PGDMP"})()


def test_restore_skips_extension_entries_and_keeps_everything_else(tmp_path):
    """The app role does not own the extensions: --clean's DROP EXTENSION and the extension
    COMMENTs fail under it. The target database is provisioned with them already."""
    run = TocRun()
    f = tmp_path / "x.dump.age"
    f.write_bytes(b"age")
    B.restore(f, into_dsn="postgresql://u:p@localhost:5433/kb_t_a", identity=tmp_path / "key.txt",
              run=run, env={"KB_PG_BIN": "/pg/bin"}, post_restore=_healthy)
    final = run.calls[-1]
    assert final[0] == "/pg/bin/pg_restore"
    assert {"--no-owner", "--no-privileges", "--clean", "--if-exists"} <= set(final)
    assert "EXTENSION" not in run.use_list
    assert "TABLE public nodes" in run.use_list and "TRIGGER public nodes nodes_notify_update" in run.use_list
    assert not Path(final[final.index("--use-list") + 1]).exists()          # list file cleaned up


def test_cli_run_requires_a_recipient(monkeypatch, capsys):
    monkeypatch.delenv("KB_BACKUP_AGE_RECIPIENT", raising=False)
    assert B.main(["run"]) == 2
    assert "KB_BACKUP_AGE_RECIPIENT" in capsys.readouterr().err


class ModeRecordingRun(FakeRun):
    """Like FakeRun, but records the mode each output file was created with (plain write_bytes,
    so the process umask decides it)."""
    def __init__(self):
        super().__init__()
        self.modes = []

    def __call__(self, argv, check=True, **kw):
        p = super().__call__(argv, check=check, **kw)
        flag = "--file" if argv[0].endswith("pg_dump") else "--output" if "--output" in argv else None
        if flag:
            self.modes.append(Path(argv[argv.index(flag) + 1]).stat().st_mode & 0o777)
        return p


def test_dump_dir_is_0700_and_no_file_is_ever_world_readable(tmp_path):
    run = ModeRecordingRun()
    out_dir = tmp_path / "custom" / "backups"                               # neither level exists yet
    old = os.umask(0o022)
    try:
        (tmp_path / "pre" / "kb_t_a").mkdir(parents=True)                   # pre-existing folder at 0755
        assert (tmp_path / "pre" / "kb_t_a").stat().st_mode & 0o777 == 0o755
        B.run_backups(_registry(), out_dir=tmp_path / "pre", recipient="age1xyz", run=FakeRun(),
                      env={"KB_PG_BIN": "/pg/bin"})
        assert (tmp_path / "pre" / "kb_t_a").stat().st_mode & 0o777 == 0o700  # tightened
        written = B.run_backups(_registry(), out_dir=out_dir, recipient="age1xyz",
                                now=datetime(2026, 10, 9, tzinfo=timezone.utc), run=run,
                                env={"KB_PG_BIN": "/pg/bin"})
        assert out_dir.stat().st_mode & 0o777 == 0o700                      # KB_BACKUP_DIR created 0700
        (out_dir / "kb_t_off").chmod(0o755)                                 # a loosened existing folder
        B.run_backups(_registry(), out_dir=out_dir, recipient="age1xyz",
                      now=datetime(2026, 10, 10, tzinfo=timezone.utc), run=run,
                      env={"KB_PG_BIN": "/pg/bin"})
        assert os.umask(0o022) == 0o022                                     # caller's umask restored
    finally:
        os.umask(old)
    assert len(run.modes) == 12 and all(m == 0o600 for m in run.modes)      # temp + .age, at creation
    assert (out_dir / "kb_t_off").stat().st_mode & 0o777 == 0o700           # tightened
    for p in written:
        assert p.parent.stat().st_mode & 0o777 == 0o700
        assert p.stat().st_mode & 0o777 == 0o600


class FailingRun(FakeRun):
    """age writes a partial output for kb_t_a, then fails."""
    def __call__(self, argv, check=True, **kw):
        p = super().__call__(argv, check=check, **kw)
        if argv[0] == "age" and "/kb_t_a/" in argv[argv.index("--output") + 1]:
            raise subprocess.CalledProcessError(1, argv)
        return p


def test_one_failing_database_does_not_stop_the_others(tmp_path, capsys):
    report = B.run_backups_report(_registry(), out_dir=tmp_path, recipient="age1xyz",
                                  now=datetime(2026, 10, 9, tzinfo=timezone.utc), run=FailingRun(),
                                  env={"KB_PG_BIN": "/pg/bin"})
    assert [p.parent.name for p in report.written] == ["kb_mcp", "kb_t_off"]
    assert [db for db, _ in report.failures] == ["kb_t_a"]
    assert report.attempted == [tmp_path / "kb_mcp", tmp_path / "kb_t_a", tmp_path / "kb_t_off"]
    assert not list((tmp_path / "kb_t_a").iterdir())                        # partial .age and .tmp gone
    assert "[kb-backup] kb_t_a: FAILED:" in capsys.readouterr().err
    written = B.run_backups(_registry(), out_dir=tmp_path / "again", recipient="age1xyz",
                            run=FailingRun(), env={"KB_PG_BIN": "/pg/bin"})
    assert [p.parent.name for p in written] == ["kb_mcp", "kb_t_off"]      # signature kept


def test_cli_run_exits_1_when_any_database_failed_and_still_prunes(tmp_path, monkeypatch, capsys):
    old = tmp_path / "kb_t_a"
    old.mkdir()
    start = datetime(2026, 10, 8, tzinfo=timezone.utc)
    for i in range(30):
        (old / B.backup_name(start - timedelta(days=i))).write_bytes(b"x")
    monkeypatch.setenv("KB_BACKUP_AGE_RECIPIENT", "age1xyz")
    monkeypatch.setenv("KB_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("KB_PG_BIN", "/pg/bin")
    monkeypatch.setattr(B, "load_registry", _registry)
    monkeypatch.setattr(B.subprocess, "run", FailingRun())
    assert B.main(["run"]) == 1
    assert len(list((tmp_path / "kb_mcp").glob("*.dump.age"))) == 1
    assert len(list((tmp_path / "kb_t_off").glob("*.dump.age"))) == 1
    assert len(list(old.glob("*.dump.age"))) < 30                           # retention ran for the failed db
    err = capsys.readouterr().err
    assert "1 database(s) FAILED" in err and "kb_t_a" in err


class SecretLeakingRun(FakeRun):
    """pg_dump fails for kb_t_a with the credentialed DSN in its argv, as the real one would."""
    def __call__(self, argv, check=True, **kw):
        if argv[0].endswith("pg_dump") and "kb_t_a" in argv[argv.index("--dbname") + 1]:
            raise subprocess.CalledProcessError(
                1, ["/pg/bin/pg_dump", "--dbname", "postgresql://u:SECRET@localhost:5433/kb_t_a"])
        if argv[0] == "age" and "/kb_t_off/" in argv[argv.index("--output") + 1]:
            raise RuntimeError("connect to postgresql://u:SECRET@localhost:5433/kb_t_off failed")
        return super().__call__(argv, check=check, **kw)


def test_failure_log_and_report_never_carry_the_dsn_password(tmp_path, capsys):
    report = B.run_backups_report(_registry(), out_dir=tmp_path, recipient="age1xyz",
                                  run=SecretLeakingRun(), env={"KB_PG_BIN": "/pg/bin"})
    err = capsys.readouterr().err
    assert "SECRET" not in err
    assert not any("SECRET" in msg for _, msg in report.failures)
    assert dict(report.failures)["kb_t_a"] == "pg_dump exited 1"
    assert dict(report.failures)["kb_t_off"].startswith("RuntimeError")
    assert "postgresql://***@localhost:5433/kb_t_off" in dict(report.failures)["kb_t_off"]


def test_cli_restore_prints_the_database_name_not_the_dsn(tmp_path, monkeypatch, capsys):
    f = tmp_path / "x.dump.age"
    f.write_bytes(b"age")
    monkeypatch.setattr(B.subprocess, "run", FakeRun())                     # resolved at call time
    monkeypatch.setattr(B, "_post_restore", _healthy)
    dsn = "postgresql://u:SECRET@localhost:5433/kb_t_a"
    assert B.main(["restore", str(f), "--into", dsn, "--identity", str(tmp_path / "k")]) == 0
    out = capsys.readouterr()
    assert "SECRET" not in out.out + out.err and "kb_t_a" in out.out


def test_cli_restore_failure_is_redacted_and_exits_1(tmp_path, monkeypatch, capsys):
    def boom(argv, **kw):
        raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(B.subprocess, "run", boom)
    dsn = "postgresql://u:SECRET@localhost:5433/kb_t_a"
    assert B.main(["restore", str(tmp_path / "x"), "--into", dsn, "--identity", str(tmp_path / "k")]) == 1
    out = capsys.readouterr()
    assert "SECRET" not in out.out + out.err and "age exited 1" in out.err


# --- restore exit status: allowlisted pg_restore errors + post-restore verification ---

# What restoring a pre-0049 dump prints with the extension TOC entries skipped (the error
# text is as captured from a real pg_restore run).
MEASURED_STDERR = """pg_restore: error: could not execute query: ERROR:  operator does not exist: public.ltree = public.ltree
LINE 1: ...ld.title IS DISTINCT FROM new.title) OR (old.path IS DISTINC...
                                                             ^
HINT:  No operator matches the given name and argument types. You might need to add explicit type casts.
Command was: CREATE TRIGGER nodes_notify_update AFTER UPDATE ON public.nodes FOR EACH ROW WHEN (((old.path IS DISTINCT FROM new.path))) EXECUTE FUNCTION public.kb_notify_change();


pg_restore: warning: errors ignored on restore: 1
"""

OTHER_ERROR = """pg_restore: error: could not execute query: ERROR:  relation "public.nodes" does not exist
Command was: COPY public.nodes (id, title) FROM stdin;
"""


class StderrRun(FakeRun):
    """The restoring pg_restore call exits `returncode` and writes `stderr`."""
    def __init__(self, returncode, stderr):
        super().__init__()
        self.returncode, self.stderr = returncode, stderr

    def __call__(self, argv, check=True, **kw):
        if argv[0].endswith("pg_restore") and "--dbname" in argv:
            self.calls.append(argv)
            return type("P", (), {"returncode": self.returncode, "stdout": b"",
                                  "stderr": self.stderr.encode()})()
        return super().__call__(argv, check=check, **kw)


def _restore(tmp_path, run, post_restore):
    f = tmp_path / "x.dump.age"
    f.write_bytes(b"age")
    return B.restore(f, into_dsn="postgresql://u:p@localhost:5433/kb_t_a", identity=tmp_path / "k",
                     run=run, env={"KB_PG_BIN": "/pg/bin"}, post_restore=post_restore)


EXTENSION_ERROR = """pg_restore: error: could not execute query: ERROR:  must be owner of extension vector
Command was: DROP EXTENSION IF EXISTS vector;
"""


def test_the_pre_0049_trigger_failure_is_the_only_allowlisted_error():
    assert [name for name, _ in B.BENIGN_RESTORE_ERRORS] == ["pre-0049 nodes_notify_update"]
    assert B.unexpected_restore_errors(1, MEASURED_STDERR) == []
    assert B.unexpected_restore_errors(0, "") == []
    # extension TOC entries are skipped, so an extension error now means something is off
    assert B.unexpected_restore_errors(1, EXTENSION_ERROR) == [
        "pg_restore: error: could not execute query: ERROR:  must be owner of extension vector"]


def test_restore_with_only_allowlisted_errors_migrates_verifies_and_succeeds(tmp_path, capsys):
    seen = []

    def post_restore(dsn):
        seen.append(dsn)
        return ["0049_notify_trigger_restorable"], []
    applied = _restore(tmp_path, StderrRun(1, MEASURED_STDERR), post_restore)
    assert applied == ["0049_notify_trigger_restorable"]
    assert seen == ["postgresql://u:p@localhost:5433/kb_t_a"]
    assert "operator does not exist: public.ltree" in capsys.readouterr().err  # still shown to the operator


def test_restore_fails_on_any_error_outside_the_allowlist_and_does_not_migrate(tmp_path, capsys):
    seen = []
    stderr = MEASURED_STDERR + OTHER_ERROR + "pg_restore: error: postgresql://u:SECRET@localhost/x\n"
    with pytest.raises(B.RestoreError) as e:
        _restore(tmp_path, StderrRun(1, stderr), lambda dsn: seen.append(dsn) or ([], []))
    assert 'relation "public.nodes" does not exist' in str(e.value) and "2 unexpected" in str(e.value)
    assert seen == []
    err = capsys.readouterr().err
    assert "COPY public.nodes" in err and "SECRET" not in err and "SECRET" not in str(e.value)


def test_restore_fails_on_a_nonzero_exit_without_a_message(tmp_path):
    with pytest.raises(B.RestoreError, match="exited 1 with no error message"):
        _restore(tmp_path, StderrRun(1, ""), _healthy)


def test_restore_fails_when_the_errors_are_benign_but_the_trigger_is_missing(tmp_path):
    with pytest.raises(B.RestoreError, match="nodes_notify_update is missing"):
        _restore(tmp_path, StderrRun(1, MEASURED_STDERR),
                 lambda dsn: ([], ["trigger nodes_notify_update is missing"]))


def _cli_restore(tmp_path, monkeypatch, run, post_restore):
    f = tmp_path / "x.dump.age"
    f.write_bytes(b"age")
    monkeypatch.setattr(B.subprocess, "run", run)
    monkeypatch.setattr(B, "_post_restore", post_restore)
    dsn = "postgresql://u:SECRET@localhost:5433/kb_t_a"
    return B.main(["restore", str(f), "--into", dsn, "--identity", str(tmp_path / "k")])


def test_cli_restore_exit_0_on_benign_errors_and_a_passing_verification(tmp_path, monkeypatch, capsys):
    rc = _cli_restore(tmp_path, monkeypatch, StderrRun(1, MEASURED_STDERR),
                      lambda dsn: (["0049_notify_trigger_restorable"], []))
    out = capsys.readouterr()
    assert rc == 0
    assert "verified; applied 1 migration(s): 0049_notify_trigger_restorable" in out.out
    assert "nodes_notify_update" in out.err and "SECRET" not in out.out + out.err


def test_cli_restore_exit_1_on_an_unknown_error_line(tmp_path, monkeypatch, capsys):
    assert _cli_restore(tmp_path, monkeypatch, StderrRun(1, MEASURED_STDERR + OTHER_ERROR), _healthy) == 1
    err = capsys.readouterr().err
    assert "RestoreError: 1 unexpected pg_restore error(s)" in err and "SECRET" not in err


def test_cli_restore_exit_1_on_benign_errors_but_the_trigger_missing(tmp_path, monkeypatch, capsys):
    rc = _cli_restore(tmp_path, monkeypatch, StderrRun(1, MEASURED_STDERR),
                      lambda dsn: ([], ["trigger nodes_notify_update is missing"]))
    assert rc == 1
    assert "post-restore check failed: trigger nodes_notify_update is missing" in capsys.readouterr().err


class ListRun(FakeRun):
    """`pg_restore --list` returns the given result; records whether a restore was attempted."""
    def __init__(self, returncode, stdout, stderr=b""):
        super().__init__()
        self.result = type("P", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()

    def __call__(self, argv, check=True, **kw):
        if argv[0].endswith("pg_restore") and "--list" in argv:
            self.calls.append(argv)
            return self.result
        return super().__call__(argv, check=check, **kw)


def test_restore_refuses_an_archive_list_without_table_data(tmp_path):
    run = ListRun(0, TOC.replace("4400; 0 722279500 TABLE DATA public nodes kb_app\n", "").encode())
    with pytest.raises(B.RestoreError, match="archive list has no table data; refusing to restore"):
        _restore(tmp_path, run, _healthy)
    assert not any("--dbname" in c for c in run.calls)                     # pg_restore never ran


def test_a_failing_archive_list_shows_its_stderr_redacted(tmp_path):
    run = ListRun(1, b"", b"pg_restore: error: input file does not appear to be a valid archive "
                         b"(postgresql://u:SECRET@localhost/x)\n")
    with pytest.raises(B.RestoreError) as e:
        _restore(tmp_path, run, _healthy)
    assert "pg_restore --list exited 1: " in str(e.value)
    assert "does not appear to be a valid archive" in str(e.value) and "SECRET" not in str(e.value)


def test_cli_restore_help_says_into_is_the_app_role_dsn(capsys):
    with pytest.raises(SystemExit):
        B.main(["restore", "-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "APP-ROLE DSN" in out and "never the admin role" in out


def _cli_env(monkeypatch, tmp_path, run):
    monkeypatch.setenv("KB_BACKUP_AGE_RECIPIENT", "age1xyz")
    monkeypatch.setenv("KB_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("KB_PG_BIN", "/pg/bin")
    monkeypatch.setattr(B, "load_registry", _registry)
    monkeypatch.setattr(B.subprocess, "run", run)


def test_cli_run_db_backs_up_only_that_database(tmp_path, monkeypatch):
    run = FakeRun()
    _cli_env(monkeypatch, tmp_path, run)
    assert B.main(["run", "--db", "kb_t_off"]) == 0                         # disabled tenants too
    assert sorted(p.relative_to(tmp_path).parts[0] for p in tmp_path.glob("*/*.dump.age")) == ["kb_t_off"]
    assert len([c for c in run.calls if c[0] == "/pg/bin/pg_dump"]) == 1


def test_cli_run_db_unknown_exits_2_naming_only_the_database(tmp_path, monkeypatch, capsys):
    run = FakeRun()
    _cli_env(monkeypatch, tmp_path, run)
    assert B.main(["run", "--db", "kb_t_nope"]) == 2
    err = capsys.readouterr().err
    assert "kb_t_nope" in err and "postgresql://" not in err and "kb_t_a" not in err
    assert run.calls == []


# --- live: the schema must survive pg_dump -> pg_restore (pg_restore runs with search_path='') ---

LIVE_DSN = os.environ.get("KB_TEST_DATABASE_URL", "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test")


def test_every_trigger_recreates_under_an_empty_search_path(pool):
    """pg_restore replays pg_get_triggerdef() output with search_path=''. A WHEN clause
    that needs an extension operator from `public` (ltree's `=` behind IS DISTINCT FROM)
    fails there, and the restored database silently lacks the trigger."""
    failures = []
    with pool.connection() as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("SET LOCAL search_path = ''")
            rows = conn.execute(
                "SELECT t.tgname, t.tgrelid::regclass::text, pg_get_triggerdef(t.oid) "
                "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE NOT t.tgisinternal AND n.nspname = 'public' ORDER BY 1").fetchall()
            rows = [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]
            assert any(name == "nodes_notify_update" for name, _, _ in rows)
            for name, table, definition in rows:
                try:
                    with conn.transaction():
                        conn.execute(f'DROP TRIGGER "{name}" ON {table}')
                        conn.execute(definition)
                except Exception as exc:
                    failures.append((name, str(exc).splitlines()[0]))
    assert failures == []


def test_dump_restore_roundtrip_is_clean_and_keeps_every_trigger(pool, tmp_path):
    pg_dump = B.pg_bin("pg_dump")
    if not Path(pg_dump).exists():
        pytest.skip(f"no pg_dump at {pg_dump}")
    target = f"kb_h1_restore_{os.getpid()}"
    parts = urlsplit(LIVE_DSN)
    maint = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))
    target_dsn = urlunsplit((parts.scheme, parts.netloc, f"/{target}", parts.query, parts.fragment))
    try:
        with psycopg.connect(maint, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{target}" TEMPLATE template0')
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("test role lacks CREATEDB; run the restore drill by hand to cover this")
    try:
        try:
            with psycopg.connect(target_dsn, autocommit=True) as conn:
                for ext in ("ltree", "vector", "pg_trgm"):         # as tenant_cli.create_database does
                    conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("test role cannot create extension vector (superuser-only), so it cannot "
                        "provision a restore target; run the restore drill by hand to cover this")

        def run(argv, **kw):                                         # age stand-ins: cp / cat
            if argv[0] == "age" and "--recipient" in argv:
                argv = ["cp", argv[-1], argv[argv.index("--output") + 1]]
            elif argv[0] == "age":
                argv = ["cat", argv[-1]]
            return subprocess.run(argv, **kw)

        dump = B.dump_encrypted(LIVE_DSN, tmp_path / "db" / "x.dump.age", recipient="r", run=run)
        B.restore(dump, into_dsn=target_dsn, identity=tmp_path / "k", run=run)  # raises unless exit 0
        q = "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1"
        with psycopg.connect(LIVE_DSN) as a, psycopg.connect(target_dsn) as b:
            src = [r[0] for r in a.execute(q).fetchall()]
            dst = [r[0] for r in b.execute(q).fetchall()]
        assert "nodes_notify_update" in dst and dst == src
    finally:
        with psycopg.connect(maint, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')


def _pre_0049(conn):
    """Put the test schema in the state a restored pre-0049 dump leaves: 0049 not recorded,
    and the trigger absent (its CREATE TRIGGER failed under pg_restore)."""
    conn.execute("DELETE FROM schema_migrations WHERE version = '0049_notify_trigger_restorable'")
    conn.execute("DROP TRIGGER nodes_notify_update ON nodes")


def test_post_restore_migration_heals_a_pre_0049_restore(pool):
    with pool.connection() as conn:
        with conn.transaction(force_rollback=True):
            _pre_0049(conn)
            applied, problems = B.heal_and_verify(conn)
            assert applied == ["0049_notify_trigger_restorable"]
            assert problems == []
            assert conn.execute("SELECT 1 AS ok FROM schema_migrations "
                                "WHERE version = '0049_notify_trigger_restorable'").fetchone()
            definition = conn.execute(
                "SELECT pg_get_triggerdef(oid) AS d FROM pg_trigger "
                "WHERE tgname = 'nodes_notify_update'").fetchone()["d"]
            assert "(old.path)::text IS DISTINCT FROM (new.path)::text" in definition


def test_post_restore_verification_reports_a_missing_trigger_and_a_lagging_head(pool):
    with pool.connection() as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("DROP TRIGGER nodes_notify_update ON nodes")       # 0049 still recorded
            applied, problems = B.heal_and_verify(conn)
            assert applied == [] and problems == ["trigger nodes_notify_update is missing"]
        with conn.transaction(force_rollback=True):
            conn.execute("INSERT INTO schema_migrations (version) VALUES ('9999_from_newer_code')")
            _, problems = B.heal_and_verify(conn)
            assert problems == [f"schema_migrations head is 9999_from_newer_code, "
                                f"the code's latest is {B.latest_migration()}; "
                                "restore with the code version that made the dump"]


def test_default_backup_dir_is_application_support_on_macos():
    home = Path("/home/u")
    assert B.default_backup_dir({}, platform="darwin", home=home) == \
        home / "Library" / "Application Support" / "kb" / "backups"


def test_default_backup_dir_follows_xdg_data_home_elsewhere():
    home = Path("/home/u")
    assert B.default_backup_dir({}, platform="linux", home=home) == \
        home / ".local" / "share" / "kb" / "backups"
    assert B.default_backup_dir({"XDG_DATA_HOME": "/data"}, platform="linux", home=home) == \
        Path("/data") / "kb" / "backups"
    # The XDG spec says a relative XDG_DATA_HOME is invalid and must be ignored.
    assert B.default_backup_dir({"XDG_DATA_HOME": "rel"}, platform="linux", home=home) == \
        home / ".local" / "share" / "kb" / "backups"


def test_cli_run_uses_the_platform_default_when_kb_backup_dir_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("KB_BACKUP_DIR", raising=False)
    monkeypatch.setenv("KB_BACKUP_AGE_RECIPIENT", "age1xyz")
    monkeypatch.setenv("KB_PG_BIN", "/pg/bin")
    monkeypatch.setattr(B, "default_backup_dir", lambda *a, **k: tmp_path / "default")
    monkeypatch.setattr(B, "load_registry", _registry)
    monkeypatch.setattr(B.subprocess, "run", FakeRun())
    assert B.main(["run"]) == 0
    assert list((tmp_path / "default" / "kb_mcp").glob("*.dump.age"))


# --- credentials never reach a child's argv (argv is world-visible in `ps`) ---

class EnvRecordingRun(FakeRun):
    """Like FakeRun, but also records the env each child would be started with."""
    def __init__(self):
        super().__init__()
        self.envs = []

    def __call__(self, argv, check=True, **kw):
        self.envs.append(kw.get("env"))
        return super().__call__(argv, check=check, **kw)


def _secret_registry():
    owner = Tenant("owner", frozenset({"m@x"}), database="kb_mcp")
    return Registry(owner, {}, env={"KB_APP_USER": "u", "KB_APP_PASSWORD": "pw-in-dsn-xyz"})


def test_pg_child_moves_the_password_from_the_dsn_into_the_child_env():
    for dsn in ("postgresql://u:pw%40x@localhost:5433/kb_t_a",
                "host=localhost port=5433 user=u password=pw@x dbname=kb_t_a"):
        argv_dsn, env = B.pg_child(dsn, base_env={"PATH": "/bin"})
        assert "pw" not in argv_dsn and "dbname=kb_t_a" in argv_dsn and "user=u" in argv_dsn
        assert env == {"PATH": "/bin", "PGPASSWORD": "pw@x"}
    argv_dsn, env = B.pg_child("postgresql://u@localhost/kb", base_env={"PATH": "/bin"})
    assert argv_dsn == "postgresql://u@localhost/kb" and env == {"PATH": "/bin"}


def test_backup_passes_the_password_by_env_never_argv(tmp_path):
    run = EnvRecordingRun()
    B.run_backups(_secret_registry(), out_dir=tmp_path, recipient="age1xyz", run=run,
                  env={"KB_PG_BIN": "/pg/bin"})
    assert not any("pw-in-dsn-xyz" in " ".join(c) for c in run.calls)
    dump = [i for i, c in enumerate(run.calls) if c[0] == "/pg/bin/pg_dump"][0]
    assert run.envs[dump]["PGPASSWORD"] == "pw-in-dsn-xyz"
    assert "dbname=kb_mcp" in run.calls[dump][run.calls[dump].index("--dbname") + 1]


def test_restore_passes_the_password_by_env_never_argv(tmp_path):
    run = EnvRecordingRun()
    f = tmp_path / "x.dump.age"
    f.write_bytes(b"age")
    B.restore(f, into_dsn="postgresql://u:pw-in-dsn-xyz@localhost:5433/kb_t_a",
              identity=tmp_path / "k", run=run, env={"KB_PG_BIN": "/pg/bin"}, post_restore=_healthy)
    assert not any("pw-in-dsn-xyz" in " ".join(c) for c in run.calls)
    final = [i for i, c in enumerate(run.calls) if "--dbname" in c][0]
    assert run.envs[final]["PGPASSWORD"] == "pw-in-dsn-xyz"


def _reset_backup(tmp_path, monkeypatch, *, host_pg_dump):
    from kb_core import reset_entities as R
    seen = []

    def fake_run(argv, check=True, stdout=None, env=None, **kw):
        seen.append((argv, env))
        stdout.write(b"-- dump\n")
    monkeypatch.setattr(R.shutil, "which", lambda name: "/usr/bin/pg_dump" if host_pg_dump else None)
    monkeypatch.setattr(R.subprocess, "run", fake_run)
    old = os.umask(0o022)
    try:
        path = R._backup("postgresql://u:pw-in-dsn-xyz@localhost:5433/kb_mcp",
                         out_dir=str(tmp_path / "backups"), container="db")
    finally:
        os.umask(old)
    return Path(path), seen


@pytest.mark.parametrize("host_pg_dump", [True, False])
def test_reset_entities_backup_is_private_and_keeps_the_password_off_argv(tmp_path, monkeypatch,
                                                                          host_pg_dump):
    path, seen = _reset_backup(tmp_path, monkeypatch, host_pg_dump=host_pg_dump)
    (argv, env), = seen
    assert not any("pw-in-dsn-xyz" in a for a in argv)
    assert env["PGPASSWORD"] == "pw-in-dsn-xyz"
    if not host_pg_dump:                                    # docker exec forwards it by name only
        assert argv[argv.index("-e") + 1] == "PGPASSWORD"
    assert path.read_bytes() == b"-- dump\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
