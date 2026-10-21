"""`kb-tenant`: provision, list, enable/disable, rotate, and delete tenants."""
from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from kb_core.backup import default_backup_dir
from kb_core.db import migrate
from kb_core.tenants import (OWNER_SLUG, SLUG_RE, Registry, RegistryError,
                             load_registry, parse_registry, registry_path)

EXTENSIONS = ("ltree", "vector", "pg_trgm")


def admin_dsn(env=None) -> str:
    """The bootstrap-role DSN used ONLY to create/drop roles, databases and extensions
    (it needs CREATEROLE and CREATEDB; the docker bootstrap superuser has both)."""
    env = os.environ if env is None else env
    if env.get("KB_ADMIN_DATABASE_URL"):
        return env["KB_ADMIN_DATABASE_URL"]
    user = env.get("POSTGRES_USER", "kb")
    pw = env.get("POSTGRES_PASSWORD", "")
    port = env.get("DB_HOST_PORT", "5433")
    return f"postgresql://{user}:{pw}@localhost:{port}/postgres"


def _with_db(dsn: str, name: str) -> str:
    p = urlsplit(dsn)
    return urlunsplit((p.scheme, p.netloc, f"/{name}", p.query, p.fragment))


def _is_tenant_name(name: str) -> bool:
    """Every role/database this module creates or drops is kb_t_<slug>: a guard so a bad
    argument can never reach DROP ROLE / DROP DATABASE for the owner's objects."""
    return name.startswith("kb_t_") and SLUG_RE.match(name[len("kb_t_"):]) is not None


def create_database(admin: str, name: str, *, password: str) -> None:
    """Provision tenant `name` (kb_t_<slug>): a LOGIN role of that name with `password`,
    and a database of that name OWNED by it, which no other role may connect to. The role
    has no other privilege anywhere: it cannot create roles or databases and does not
    inherit. Extensions are created here, by the admin role, because the tenant role
    cannot."""
    if not _is_tenant_name(name):
        raise SystemExit(f"refusing to provision {name!r}: not a kb_t_<slug> name")
    with psycopg.connect(admin, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            raise SystemExit(f"database {name} already exists")
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
            raise SystemExit(f"role {name} already exists (left by an earlier tenant? "
                             f"DROP ROLE {name} as the admin role once you have checked)")
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                             "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS")
                     .format(sql.Identifier(name), sql.Literal(password)))
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0")
                         .format(sql.Identifier(name), sql.Identifier(name)))
            # PUBLIC gets CONNECT and TEMPORARY on every new database by default; only the
            # owning role (and superusers) may connect to a tenant's.
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC")
                         .format(sql.Identifier(name)))
        with psycopg.connect(_with_db(admin, name), autocommit=True) as conn:
            for ext in EXTENSIONS:
                conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}")
                             .format(sql.Identifier(ext)))
    except BaseException:
        _drop_quietly(admin, name)
        raise


def restrict_owner_database(admin: str, owner_dsn: str) -> tuple[str, str]:
    """Close the owner's database to tenant roles. PUBLIC holds CONNECT on it by default,
    so without this every tenant role could open the owner's database (its tables are not
    granted to PUBLIC, but its catalogs and any PUBLIC-granted object are). The owner's app
    role is granted CONNECT explicitly first, so it never depends on PUBLIC. Idempotent.
    Returns (database, app role)."""
    params = conninfo_to_dict(owner_dsn)
    database, role = params.get("dbname") or "", params.get("user") or ""
    if not database or not role:
        raise SystemExit("cannot tell the owner's database and role from KB_DATABASE_URL")
    if _is_tenant_name(database):
        raise SystemExit(f"KB_DATABASE_URL names a tenant database ({database}); run "
                         "kb-tenant with the owner's environment")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}")
                     .format(sql.Identifier(database), sql.Identifier(role)))
        conn.execute(sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC")
                     .format(sql.Identifier(database)))
    return database, role


def _drop_quietly(admin: str, name: str) -> None:
    """Cleanup after a failed provision: never let a failing drop mask the real error."""
    try:
        drop_database(admin, name)
    except Exception as exc:
        print(f"WARNING: could not drop {name} after a failed create: {exc}", file=sys.stderr)


def drop_database(admin: str, name: str) -> None:
    """Drop tenant database `name` and its role of the same name (kb_t_<slug> only)."""
    if not _is_tenant_name(name):
        raise SystemExit(f"refusing to drop {name!r}: not a kb_t_<slug> name")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        # WITH (FORCE) (PG 13+): a service reconnecting between the sweep and the drop
        # cannot block it.
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                     .format(sql.Identifier(name)))
        # The role owned only that database, so nothing else depends on it.
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def set_role_password(admin: str, name: str, password: str) -> None:
    if not _is_tenant_name(name):
        raise SystemExit(f"refusing to alter {name!r}: not a kb_t_<slug> name")
    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
            raise SystemExit(f"role {name} does not exist")
        conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}")
                     .format(sql.Identifier(name), sql.Literal(password)))


def migrate_database(dsn: str) -> list[str]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return migrate(conn)


def dump_registry(data: dict) -> str:
    """Minimal TOML writer for the flat registry schema (stdlib has no writer)."""
    def q(v) -> str:
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    def val(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, list):
            return "[" + ", ".join(q(x) for x in v) + "]"
        return q(v)

    out: list[str] = []
    if data.get("owner"):
        out.append("[owner]")
        out += [f"{k} = {val(v)}" for k, v in data["owner"].items()]
        out.append("")
    for slug, block in (data.get("tenant") or {}).items():
        out.append(f"[tenant.{slug}]")
        out += [f"{k} = {val(v)}" for k, v in block.items()]
        out.append("")
    return "\n".join(out)


def read_registry_file(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_registry_file(path: Path, data: dict) -> None:
    parse_registry(data, base=path.parent.resolve(), env=os.environ)   # validate before writing
    tmp = path.with_suffix(".toml.tmp")
    # Created 0600 (not under the umask) so the secrets never exist on disk world-readable;
    # fchmod covers a stale .tmp left by an earlier crash, which O_CREAT would not re-mode.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(dump_registry(data))
    tmp.replace(path)
    path.chmod(0o600)


def backup_fresh(backup_dir: Path, database: str, *, max_age_s: float = 86400) -> Path | None:
    d = backup_dir / database
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.dump.age"), key=lambda f: f.stat().st_mtime, reverse=True)
    if files and time.time() - files[0].stat().st_mtime <= max_age_s:
        return files[0]
    return None


REPO_URL_PLACEHOLDER = "<repo-url>"
SSH_USER = "git"
# scp-like `[user[:password]@]host:path` (the path must not start with `/`, or it is a local path).
_SCP_REMOTE_RE = re.compile(
    r"^(?:(?P<user>[^@/:]+)(?::(?P<password>[^@/]*))?@)?(?P<host>[\w.-]+):(?P<path>[^/].*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git(*args: str) -> str | None:
    """One read-only git query in the checkout this module runs from; None when there is no
    git, no repository, or the query fails (e.g. an installed, non-editable copy)."""
    try:
        p = subprocess.run(["git", *args], cwd=Path(__file__).resolve().parent,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    out = p.stdout.strip()
    return out if p.returncode == 0 and out else None


def origin_remote() -> str | None:
    """`git remote get-url origin`, or None."""
    return _git("remote", "get-url", "origin")


def head_commit() -> str | None:
    """`git rev-parse HEAD` (a full sha), or None."""
    sha = _git("rev-parse", "HEAD")
    return sha if sha and _SHA_RE.match(sha) else None


def _ssh_user(user: str | None, password: str | None) -> str:
    """The ssh login to print: never a password, and never a user that came with one
    (that pair is a credential, not a login name)."""
    return user if user and password is None else SSH_USER


def agent_git_url(remote: str | None) -> str:
    """The `git+ssh://` URL a tenant installs the agent from. The `ego-os-agent`
    distribution (CLI `kb-agent`) installs from source; this project does not publish it
    to PyPI, and a bare name would resolve there instead, so the bundle only ever names
    the repository. No credential embedded in the remote ever reaches the bundle: an https
    remote becomes ssh as `git@host` (collaborators read the private repo over ssh), and an
    ssh or scp-form remote keeps its login name only when it carries no password. Anything
    else, or no remote, yields a placeholder for the operator to fill in."""
    if remote:
        m = _SCP_REMOTE_RE.match(remote)
        if m and "://" not in remote:
            return f"git+ssh://{_ssh_user(m['user'], m['password'])}@{m['host']}/{m['path']}"
        parts = urlsplit(remote)
        path = parts.path.lstrip("/")
        if parts.scheme in ("ssh", "git+ssh") and parts.hostname and path:
            port = f":{parts.port}" if parts.port else ""
            user = _ssh_user(parts.username, parts.password)
            return f"git+ssh://{user}@{parts.hostname}{port}/{path}"
        if parts.scheme in ("https", "http") and parts.hostname and path.strip("/"):
            return f"git+ssh://{SSH_USER}@{parts.hostname}/{path}"
    return f"git+{REPO_URL_PLACEHOLDER}"


def agent_install_command(remote: str | None, rev: str | None = None) -> str:
    """The tenant's install line, pinned to `rev` (the owner's deployed commit) when known,
    so the agent matches the server it talks to. The commit must be pushed to the remote.
    The name before `@` is the distribution name and must match KB_Agent's pyproject:
    uv refuses a direct reference whose name differs from the built metadata."""
    pin = f"@{rev}" if rev else ""
    return f'uv tool install "ego-os-agent @ {agent_git_url(remote)}{pin}#subdirectory=KB_Agent"'


def _registry_data() -> tuple[Path, dict]:
    p = registry_path()
    return p, read_registry_file(p)


def _pin_owner_emails(data: dict, env=None) -> list[str]:
    """With no [owner] emails, the registry falls back to KB_CF_ACCESS_EMAILS, so any address
    ever added to that env list silently becomes the OWNER on every surface.
    Write the list down (trimmed, lowercased, de-duplicated) the first time a tenant is
    created; an existing [owner] emails is never touched. Returns what was pinned."""
    env = os.environ if env is None else env
    owner = data.get("owner") or {}
    if "emails" in owner:
        return []
    emails: list[str] = []
    for e in env.get("KB_CF_ACCESS_EMAILS", "").split(","):
        e = e.strip().lower()
        if e and e not in emails:
            emails.append(e)
    if emails:
        data["owner"] = {"emails": emails, **owner}
    return emails


def cmd_create(args) -> int:
    if not SLUG_RE.match(args.slug) or args.slug == OWNER_SLUG:
        print(f"bad slug {args.slug!r}: must match {SLUG_RE.pattern}", file=sys.stderr)
        return 2
    path, data = _registry_data()
    if args.slug in (data.get("tenant") or {}):
        print(f"tenant {args.slug} already exists in {path}", file=sys.stderr)
        return 1
    secret = secrets.token_hex(32)
    database = f"kb_t_{args.slug}"
    block = {"emails": [e.strip().lower() for e in args.email], "database": database,
             "agent_secret": secret, "db_password": secrets.token_hex(32),
             "vault_layer": args.vault_layer, "enabled": True}
    data.setdefault("tenant", {})[args.slug] = block
    pinned = _pin_owner_emails(data)
    try:
        owner, tenants = parse_registry(data, base=path.parent.resolve(), env=os.environ)
    except RegistryError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    tenant = tenants[args.slug]
    registry = Registry(owner, tenants, env=os.environ)
    dsn = registry.dsn_for(tenant)
    if not args.no_create_db:
        owner_db, owner_role = restrict_owner_database(admin_dsn(), registry.dsn_for(owner))
        print(f"{owner_db}: CONNECT limited to {owner_role} and superusers")
        create_database(admin_dsn(), database, password=tenant.db_password)
    try:
        if not args.no_create_db:
            applied = migrate_database(dsn)
            print(f"created {database}: {len(applied)} migration(s) applied")
        # mkdir before the registry write, so the write is the last step that can fail:
        # a failure never leaves a registry entry pointing at a dropped database.
        (path.parent / "tenants" / args.slug).mkdir(parents=True, exist_ok=True)
        write_registry_file(path, data)
    except BaseException:
        if not args.no_create_db:
            _drop_quietly(admin_dsn(), database)
        raise
    if pinned:
        print(f"pinned [owner] emails in {path} from KB_CF_ACCESS_EMAILS: {len(pinned)} "
              "address(es); the registry, not the env list, now names the owner")
    elif not (data.get("owner") or {}).get("emails"):
        print(f"WARNING: no owner emails: {path} has no [owner] emails and KB_CF_ACCESS_EMAILS "
              "is empty — add `emails = [...]` under [owner]", file=sys.stderr)
    gateway = os.environ.get("KB_GATEWAY_URL", "https://<agent-host>")
    print("\n=== onboarding bundle (send over a private channel) ===")
    print(f"tenant        = {args.slug}")
    print(f"agent_secret  = {secret}")
    print(f"gateway       = {gateway}")
    print(f"install       = {agent_install_command(origin_remote(), head_commit())}")
    print(f"init          = kb-agent init --gateway {gateway} --tenant {args.slug} "
          "--cf-client-id <service token client id> --cf-client-secret <service token client secret> "
          "--vault ~/path/to/vault   (prompts for the agent_secret)")
    print("edge          = add the emails to the MCP + dashboard Access policies; create an "
          f"Access service token named {args.slug} (docs/ops/tenant-onboarding.md)")
    return 0


def cmd_list(_args) -> int:
    reg = load_registry()
    for t in reg.tenants(include_disabled=True):
        print(f"{t.slug:<12} {'on ' if t.enabled else 'off'}  {t.database:<20} {', '.join(sorted(t.emails))}")
    return 0


def _set_enabled(slug: str, value: bool) -> int:
    path, data = _registry_data()
    if slug not in (data.get("tenant") or {}):
        print(f"unknown tenant {slug}", file=sys.stderr)
        return 2
    data["tenant"][slug]["enabled"] = value
    write_registry_file(path, data)
    print(f"{slug}: enabled = {value}")
    return 0


def cmd_rotate(args) -> int:
    path, data = _registry_data()
    if args.slug not in (data.get("tenant") or {}):
        print(f"unknown tenant {args.slug}", file=sys.stderr)
        return 2
    data["tenant"][args.slug]["agent_secret"] = secrets.token_hex(32)
    write_registry_file(path, data)
    print(f"{args.slug}: new agent_secret = {data['tenant'][args.slug]['agent_secret']}")
    return 0


def cmd_delete(args) -> int:
    path, data = _registry_data()
    # Validate BEFORE anything destructive: the database to drop comes from the parsed
    # registry (forced to kb_t_<slug>), never from raw TOML, and an invalid sibling entry
    # is caught here rather than by the post-drop registry write.
    try:
        _owner, tenants = parse_registry(data, base=path.parent.resolve(), env=os.environ)
    except RegistryError as exc:
        print(f"refusing: {path} is invalid, fix it first: {exc}", file=sys.stderr)
        return 2
    if args.slug not in tenants:
        print(f"unknown tenant {args.slug}", file=sys.stderr)
        return 2
    database = tenants[args.slug].database
    backup_dir = Path(os.environ.get("KB_BACKUP_DIR") or default_backup_dir())
    if not args.no_backup:
        fresh = backup_fresh(backup_dir, database)
        if fresh is None:
            print(f"refusing: no backup of {database} newer than 24 h under {backup_dir / database} "
                  "(run kb-backup, or pass --no-backup)", file=sys.stderr)
            return 1
        print(f"backup on hand: {fresh}")
    drop_database(admin_dsn(), database)
    del data["tenant"][args.slug]
    write_registry_file(path, data)
    print(f"dropped database and role {database}; registry entry removed; backups under "
          f"{backup_dir / database} kept")
    return 0


def cmd_create_db(args) -> int:
    """Role + EMPTY database for an existing registry entry, with the entry's password and
    no migrations: the target of a restore (docs/ops/backup-restore.md §5.3 B), or of an
    entry made with `create --no-create-db`."""
    path, data = _registry_data()
    try:
        owner, tenants = parse_registry(data, base=path.parent.resolve(), env=os.environ)
    except RegistryError as exc:
        print(f"refusing: {path} is invalid, fix it first: {exc}", file=sys.stderr)
        return 2
    if args.slug not in tenants:
        print(f"unknown tenant {args.slug}", file=sys.stderr)
        return 2
    t = tenants[args.slug]
    registry = Registry(owner, tenants, env=os.environ)
    owner_db, owner_role = restrict_owner_database(admin_dsn(), registry.dsn_for(owner))
    print(f"{owner_db}: CONNECT limited to {owner_role} and superusers")
    create_database(admin_dsn(), t.database, password=t.db_password)
    print(f"created role and empty database {t.database} (no migrations applied)")
    return 0


def cmd_rotate_db_password(args) -> int:
    """A new password for the tenant's database role. Services hold pooled connections
    opened with the old one and open new ones from the registry they loaded at start:
    restart them afterwards."""
    path, data = _registry_data()
    try:
        _owner, tenants = parse_registry(data, base=path.parent.resolve(), env=os.environ)
    except RegistryError as exc:
        print(f"refusing: {path} is invalid, fix it first: {exc}", file=sys.stderr)
        return 2
    if args.slug not in tenants:
        print(f"unknown tenant {args.slug}", file=sys.stderr)
        return 2
    password = secrets.token_hex(32)
    set_role_password(admin_dsn(), tenants[args.slug].db_role, password)
    data["tenant"][args.slug]["db_password"] = password
    write_registry_file(path, data)
    print(f"{args.slug}: new database password written to {path}; restart the services")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb-tenant", description="Provision and manage tenants")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("slug")
    c.add_argument("--email", action="append", required=True)
    c.add_argument("--vault-layer", default="Obsidian")
    c.add_argument("--no-create-db", action="store_true")
    c.set_defaults(fn=cmd_create)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    e = sub.add_parser("enable"); e.add_argument("slug"); e.set_defaults(fn=lambda a: _set_enabled(a.slug, True))
    d = sub.add_parser("disable"); d.add_argument("slug"); d.set_defaults(fn=lambda a: _set_enabled(a.slug, False))
    r = sub.add_parser("rotate-secret"); r.add_argument("slug"); r.set_defaults(fn=cmd_rotate)
    cd = sub.add_parser("create-db"); cd.add_argument("slug"); cd.set_defaults(fn=cmd_create_db)
    p = sub.add_parser("rotate-db-password"); p.add_argument("slug")
    p.set_defaults(fn=cmd_rotate_db_password)
    x = sub.add_parser("delete"); x.add_argument("slug"); x.add_argument("--yes", action="store_true")
    x.add_argument("--no-backup", action="store_true"); x.set_defaults(fn=cmd_delete)
    s = sub.add_parser("dsn"); s.add_argument("slug")
    s.set_defaults(fn=lambda a: __import__("kb_core.tenants", fromlist=["main"]).main(["dsn", a.slug]))
    args = ap.parse_args(argv)
    if args.cmd == "delete" and not args.yes:
        print("delete is destructive: pass --yes", file=sys.stderr)
        return 2
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
