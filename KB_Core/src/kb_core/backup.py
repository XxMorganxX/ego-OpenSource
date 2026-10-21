"""`kb-backup`: nightly encrypted per-database dumps + restore.

Every database in the registry (owner + every tenant, disabled ones included) is dumped
with pg_dump -Fc and encrypted with `age` to a recipient key whose PRIVATE half lives off
the host. Retention keeps 14 daily files then one per ISO week for 8 weeks.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from kb_core.db import MIGRATIONS_DIR, migrate
from kb_core.tenants import Registry, load_registry


def default_backup_dir(env=None, *, platform: str | None = None, home: Path | None = None) -> Path:
    """Where backups go when KB_BACKUP_DIR is unset: ~/Library/Application Support/kb/backups
    on macOS, $XDG_DATA_HOME/kb/backups (default ~/.local/share/kb/backups) elsewhere."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else home
    if platform == "darwin":
        return home / "Library" / "Application Support" / "kb" / "backups"
    xdg = env.get("XDG_DATA_HOME", "")
    base = Path(xdg) if os.path.isabs(xdg) else home / ".local" / "share"
    return base / "kb" / "backups"


STAMP = "%Y%m%dT%H%M%SZ"


def pg_bin(name: str, env=None) -> str:
    env = os.environ if env is None else env
    if env.get("KB_PG_BIN"):
        return str(Path(env["KB_PG_BIN"]) / name)
    return shutil.which(name) or f"/opt/homebrew/opt/postgresql@16/bin/{name}"


def backup_name(now: datetime) -> str:
    return f"{now.astimezone(timezone.utc):{STAMP}}.dump.age"


def _stamp(path: Path) -> datetime:
    return datetime.strptime(path.name.split(".")[0], STAMP).replace(tzinfo=timezone.utc)


_CREDENTIALS = re.compile(r"//[^@/\s]+@")


def _redact(text: str) -> str:
    """Strip the `user:password@` segment from every URL in `text`."""
    return _CREDENTIALS.sub("//***@", text)


def _describe_failure(exc: BaseException) -> str:
    """A loggable one-liner for a failed dump/restore. Never echoes a subprocess argv: the
    argv carries the credentialed DSN, and these lines land in the launchd log."""
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = exc.cmd[0] if isinstance(exc.cmd, (list, tuple)) and exc.cmd else str(exc.cmd).split()[0]
        return f"{Path(cmd).name} exited {exc.returncode}"
    return _redact(f"{type(exc).__name__}: {exc}")


def pg_child(dsn: str, base_env=None) -> tuple[str, dict]:
    """Split a DSN for a pg_dump/pg_restore/psql child: returns (dsn without the password,
    env with PGPASSWORD set). A child's argv is readable by every user on the host via `ps`;
    its environment is not. A DSN without a password comes back unchanged."""
    env = dict(os.environ if base_env is None else base_env)
    params = conninfo_to_dict(dsn)
    password = params.pop("password", None)
    if password is None:
        return dsn, env
    env["PGPASSWORD"] = str(password)
    return make_conninfo(**params), env


def _database_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/") or "?"


def dump_encrypted(dsn: str, out: Path, *, recipient: str, run=None, env=None) -> Path:
    """Dump + encrypt one database. KB_BACKUP_DIR (when created here) and the per-database
    dir are 0700 (an existing per-database dir is tightened), and both the plaintext temp
    and the .age file are created under umask 0077, so neither is ever readable by others,
    wherever KB_BACKUP_DIR points. On failure no partial .age is left behind."""
    run = subprocess.run if run is None else run
    if not out.parent.parent.exists():
        out.parent.parent.mkdir(mode=0o700, parents=True)
    out.parent.mkdir(mode=0o700, exist_ok=True)
    out.parent.chmod(0o700)
    tmp = out.with_name(out.name + ".tmp")
    argv_dsn, child_env = pg_child(dsn)
    old_umask = os.umask(0o077)
    try:
        run([pg_bin("pg_dump", env), "--format=custom", "--no-owner", "--no-privileges",
             "--dbname", argv_dsn, "--file", str(tmp)], check=True, env=child_env)
        run(["age", "--recipient", recipient, "--output", str(out), str(tmp)], check=True)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    finally:
        os.umask(old_umask)
        tmp.unlink(missing_ok=True)
    out.chmod(0o600)
    return out


@dataclass
class BackupReport:
    written: list[Path] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)   # (database, error)
    attempted: list[Path] = field(default_factory=list)             # per-database dirs


def run_backups_report(registry: Registry, *, out_dir: Path, recipient: str,
                       now: datetime | None = None, run=None, env=None,
                       database: str | None = None) -> BackupReport:
    """Back up every database (or only `database`); one database failing is logged and
    recorded, never fatal to the rest. `env` steers tool lookup only; each DSN comes from the
    registry's own env, which carries the database credentials."""
    run = subprocess.run if run is None else run
    now = now or datetime.now(timezone.utc)
    report = BackupReport()
    for t in registry.tenants(include_disabled=True):
        if database is not None and t.database != database:
            continue
        out = out_dir / t.database / backup_name(now)
        report.attempted.append(out.parent)
        try:
            report.written.append(
                dump_encrypted(registry.dsn_for(t), out, recipient=recipient, run=run, env=env))
        except Exception as exc:
            reason = _describe_failure(exc)
            print(f"[kb-backup] {t.database}: FAILED: {reason}", file=sys.stderr)
            report.failures.append((t.database, reason))
    return report


def run_backups(registry: Registry, *, out_dir: Path, recipient: str, now: datetime | None = None,
                run=None, env=None) -> list[Path]:
    return run_backups_report(registry, out_dir=out_dir, recipient=recipient, now=now,
                              run=run, env=env).written


def apply_retention(db_dir: Path, *, daily: int = 14, weekly: int = 8) -> list[Path]:
    files = sorted(db_dir.glob("*.dump.age"), key=_stamp, reverse=True)
    keep = set(files[:daily])
    weeks: set[tuple[int, int]] = set()
    for f in files[daily:]:
        wk = _stamp(f).isocalendar()[:2]
        if wk not in weeks and len(weeks) < weekly:
            weeks.add(wk)
            keep.add(f)
    removed = [f for f in files if f not in keep]
    for f in removed:
        f.unlink()
    return removed


# `pg_restore --list` lines for the extensions and their comments, e.g.
# `2; 3079 722277577 EXTENSION - ltree` and `4216; 0 0 COMMENT - EXTENSION ltree`.
_EXTENSION_TOC = re.compile(rb"^\d+; \d+ \d+ (?:EXTENSION|COMMENT - EXTENSION) ")
# e.g. `4400; 0 722279500 TABLE DATA public nodes kb_app`
_TABLE_DATA_TOC = re.compile(rb"^\d+; \d+ \d+ TABLE DATA ")


def _text(raw) -> str:
    raw = raw or b""
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw

# pg_restore errors that do NOT mean the restored database is incomplete. `restore` succeeds
# only when every pg_restore error matches one of these AND the post-restore check passes
# (migrations at the code's head, nodes_notify_update present). Anything else fails it.
# (The extensions' "must be owner" errors never reach this list: `restore` drops those TOC
# entries, see _EXTENSION_TOC. If one appears anyway it is unexpected and fails the restore.)
BENIGN_RESTORE_ERRORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Dumps taken before migration 0049 (kept for ~10 weeks by retention) define the trigger
    # with `OLD.path IS DISTINCT FROM NEW.path`. That needs ltree's `=` from `public`, which
    # pg_restore's empty search_path cannot find, so the CREATE TRIGGER fails. Such a dump's
    # schema_migrations lacks 0049, so the post-restore migration run applies 0049 and that
    # recreates the trigger. The post-restore check then requires the trigger to exist.
    ("pre-0049 nodes_notify_update",
     re.compile(r"ERROR:\s+operator does not exist: public\.ltree = public\.ltree\b.*?"
                r"Command was: CREATE TRIGGER nodes_notify_update ", re.S)),
)


class RestoreError(RuntimeError):
    """The restore is incomplete or unverified; the database must not go back into service."""


def _error_blocks(stderr: str) -> list[str]:
    """Each `pg_restore: error: ...` line together with its continuation lines
    (`Command was: ...`, LINE/HINT), up to the next `pg_restore:` line."""
    blocks: list[str] = []
    cur: list[str] | None = None
    for line in stderr.splitlines():
        if line.startswith("pg_restore:"):
            if cur is not None:
                blocks.append("\n".join(cur))
            cur = [line] if line.startswith("pg_restore: error:") else None
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        blocks.append("\n".join(cur))
    return blocks


def unexpected_restore_errors(returncode: int, stderr: str) -> list[str]:
    """pg_restore errors that are not in BENIGN_RESTORE_ERRORS (first line of each)."""
    blocks = _error_blocks(stderr)
    if returncode != 0 and not blocks:
        return [f"pg_restore exited {returncode} with no error message"]
    return [b.splitlines()[0] for b in blocks
            if not any(rx.search(b) for _, rx in BENIGN_RESTORE_ERRORS)]


def latest_migration() -> str:
    return max(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))


def heal_and_verify(conn: psycopg.Connection) -> tuple[list[str], list[str]]:
    """Run the migration runner the daemons use on startup (heals dumps older than the code,
    e.g. a pre-0049 trigger), then verify. Returns (applied, problems); `conn` must use
    dict rows."""
    applied = migrate(conn)
    problems: list[str] = []
    head = conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"]
    if head != latest_migration():
        problems.append(f"schema_migrations head is {head}, the code's latest is {latest_migration()}; "
                        "restore with the code version that made the dump")
    trigger = conn.execute(
        "SELECT 1 AS ok FROM pg_trigger WHERE tgname = 'nodes_notify_update' "
        "AND tgrelid = 'public.nodes'::regclass").fetchone()
    if trigger is None:
        problems.append("trigger nodes_notify_update is missing")
    return applied, problems


def _post_restore(dsn: str) -> tuple[list[str], list[str]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return heal_and_verify(conn)


def restore(path: Path, *, into_dsn: str, identity: Path, run=None, env=None,
            post_restore=None) -> list[str]:
    """Decrypt `path`, restore it into `into_dsn`, migrate, verify. Returns the migrations the
    post-restore run applied.

    `into_dsn` must be the APP-ROLE DSN (`kb-tenant dsn <slug>`), never the admin role: with
    --no-owner the restored objects belong to whoever connects, and --no-privileges does not
    replay the dump's GRANTs, so admin-owned objects would leave the daemons without access.

    Raises RestoreError when the archive list fails or carries no table data, when any
    pg_restore error is outside BENIGN_RESTORE_ERRORS, or when the post-restore check fails.
    pg_restore's stderr is shown (redacted) either way.

    The extensions' TOC entries are skipped. The admin role owns them, so --clean's
    DROP EXTENSION and the extensions' COMMENTs always fail under the app role, and the
    target database is provisioned with them already (tenant_cli.create_database)."""
    run = subprocess.run if run is None else run
    post_restore = _post_restore if post_restore is None else post_restore
    pg_restore = pg_bin("pg_restore", env)
    dec = run(["age", "--decrypt", "--identity", str(identity), str(path)], check=True, capture_output=True)
    toc = run([pg_restore, "--list"], input=dec.stdout, check=False, capture_output=True)
    if toc.returncode != 0:
        detail = _redact(_text(getattr(toc, "stderr", b"")).strip()) or "no message"
        raise RestoreError(f"pg_restore --list exited {toc.returncode}: {detail}")
    keep = [line for line in toc.stdout.splitlines() if not _EXTENSION_TOC.match(line)]
    if not any(_TABLE_DATA_TOC.match(line) for line in keep):
        # A list that parses but carries no data would "restore" nothing; migrate would then
        # build an empty schema and the verification would pass on an empty database.
        raise RestoreError("archive list has no table data; refusing to restore")
    fd, use_list = tempfile.mkstemp(prefix="kb-restore-", suffix=".list")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"\n".join(keep) + b"\n")
        argv_dsn, child_env = pg_child(into_dsn)
        proc = run([pg_restore, "--no-owner", "--no-privileges", "--clean", "--if-exists",
                    "--use-list", use_list, "--dbname", argv_dsn],
                   input=dec.stdout, check=False, capture_output=True, env=child_env)
    finally:
        os.unlink(use_list)
    stderr = _text(getattr(proc, "stderr", b""))
    if stderr.strip():
        print(_redact(stderr.rstrip("\n")), file=sys.stderr)
    unexpected = unexpected_restore_errors(proc.returncode, stderr)
    if unexpected:
        raise RestoreError(f"{len(unexpected)} unexpected pg_restore error(s): "
                           + _redact("; ".join(unexpected)))
    applied, problems = post_restore(into_dsn)
    if problems:
        raise RestoreError("post-restore check failed: " + _redact("; ".join(problems)))
    return applied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb-backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument(
        "--db", metavar="DATABASE", help="back up only this registry database (e.g. kb_t_<slug>)")
    r = sub.add_parser("restore"); r.add_argument("file")
    r.add_argument("--into", required=True, metavar="APP_ROLE_DSN",
                   help="the target database's APP-ROLE DSN (`kb-tenant dsn <slug>`), never the admin "
                        "role: restored objects are owned by the connecting role and grants are not replayed")
    r.add_argument("--identity", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "restore":
        db = _database_name(args.into)
        try:
            applied = restore(Path(args.file), into_dsn=args.into, identity=Path(args.identity))
        except Exception as exc:
            print(f"[kb-backup] restore into {db}: FAILED: {_describe_failure(exc)}", file=sys.stderr)
            return 1
        print(f"restored {args.file} into database {db}; verified; applied {len(applied)} migration(s)"
              + (f": {', '.join(applied)}" if applied else ""))
        return 0
    recipient = os.environ.get("KB_BACKUP_AGE_RECIPIENT", "").strip()
    if not recipient:
        print("KB_BACKUP_AGE_RECIPIENT is unset: generate a key with `age-keygen -o ~/kb-backup-key.txt` "
              "(outside the repo), then move that file OFF this machine, and put its public key in "
              "KB_Core/.env", file=sys.stderr)
        return 2
    out_dir = Path(os.environ.get("KB_BACKUP_DIR") or default_backup_dir())
    registry = load_registry()
    if args.db is not None and args.db not in {t.database for t in registry.tenants(include_disabled=True)}:
        print(f"[kb-backup] no registry database named {args.db}", file=sys.stderr)
        return 2
    report = run_backups_report(registry, out_dir=out_dir, recipient=recipient, database=args.db)
    removed = [p for d in report.attempted if d.is_dir() for p in apply_retention(d)]
    print(f"[kb-backup] wrote {len(report.written)} file(s) under {out_dir}; pruned {len(removed)}")
    if report.failures:
        print(f"[kb-backup] {len(report.failures)} database(s) FAILED: "
              + ", ".join(db for db, _ in report.failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
