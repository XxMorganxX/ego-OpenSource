import contextlib
import os
import re
import time
import tomllib
from pathlib import Path

import psycopg
import pytest

from kb_core import tenant_cli as C
from kb_core.tenants import load_registry
from tests.conftest import TEST_DATABASE_URL

ADMIN = os.environ.get("KB_TEST_ADMIN_DATABASE_URL")


def _admin_or_skip() -> str:
    if not ADMIN:
        pytest.skip("KB_TEST_ADMIN_DATABASE_URL unset")
    try:
        with psycopg.connect(ADMIN, connect_timeout=3, autocommit=True) as c:
            c.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"admin DSN unreachable: {exc}")
    return ADMIN


def _cleanup(admin: str, database: str) -> None:
    """Live tests never leave a kb_t_z* database on the shared cluster, pass or fail."""
    with contextlib.suppress(Exception):
        C.drop_database(admin, database)


def test_dump_registry_round_trips_through_tomllib():
    data = {"owner": {"emails": ["m@example.com"], "database": "kb_mcp"},
            "tenant": {"alice": {"emails": ["a@example.com"], "database": "kb_t_alice",
                                 "agent_secret": "s" * 64, "vault_layer": "Obsidian",
                                 "enabled": True}}}
    assert tomllib.loads(C.dump_registry(data)) == data


# The agent (distribution `ego-os-agent`, CLI `kb-agent`) installs from source; this project
# does not publish it to PyPI. A bare `uv tool install ego-os-agent` (or the old
# `kb-agent`) would resolve the name on PyPI, so the bundle must never print either.
BARE_PYPI_INSTALL = re.compile(r"uv tool install (ego-os-agent|kb-agent)(\s|$)", re.M)


@pytest.mark.parametrize("remote, url", [
    ("git@github.com:Org/repo.git", "git+ssh://git@github.com/Org/repo.git"),
    ("ssh://git@github.com/Org/repo.git", "git+ssh://git@github.com/Org/repo.git"),
    ("git+ssh://git@github.com/Org/repo", "git+ssh://git@github.com/Org/repo"),
    ("https://github.com/Org/repo", "git+ssh://git@github.com/Org/repo"),
    # Credentials in an https remote never reach the bundle.
    ("https://user:tok3n@github.com/Org/repo.git", "git+ssh://git@github.com/Org/repo.git"),
    # ...nor in an ssh or scp-form one: a user that came with a password is a credential too.
    ("ssh://deploy:tok3n@git.example.com:2222/Org/repo.git",
     "git+ssh://git@git.example.com:2222/Org/repo.git"),
    ("ssh://deploy@git.example.com/Org/repo.git", "git+ssh://deploy@git.example.com/Org/repo.git"),
    ("deploy:tok3n@github.com:Org/repo.git", "git+ssh://git@github.com/Org/repo.git"),
    ("github.com:Org/repo.git", "git+ssh://git@github.com/Org/repo.git"),
    ("/Users/me/repo", "git+<repo-url>"),
    ("file:///Users/me/repo", "git+<repo-url>"),
    (None, "git+<repo-url>"),
])
def test_agent_git_url_names_the_repository_never_a_package(remote, url):
    assert C.agent_git_url(remote) == url
    cmd = C.agent_install_command(remote)
    assert cmd == f'uv tool install "ego-os-agent @ {url}#subdirectory=KB_Agent"'
    assert not BARE_PYPI_INSTALL.search(cmd)
    assert "tok3n" not in cmd


def test_install_line_names_kb_agents_distribution():
    """`uv tool install "<name> @ git+…"` fails when <name> differs from the built metadata,
    so the bundle's name must track KB_Agent's pyproject (renamed kb-agent -> ego-os-agent)."""
    pyproject = Path(__file__).resolve().parents[2] / "KB_Agent" / "pyproject.toml"
    dist = tomllib.loads(pyproject.read_text())["project"]["name"]
    assert C.agent_install_command(None).startswith(f'uv tool install "{dist} @ ')


def test_install_line_pins_the_deployed_commit():
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert C.agent_install_command("git@github.com:Org/repo.git", sha) == (
        f'uv tool install "ego-os-agent @ git+ssh://git@github.com/Org/repo.git@{sha}#subdirectory=KB_Agent"')
    assert C.agent_install_command(None, sha) == (
        f'uv tool install "ego-os-agent @ git+<repo-url>@{sha}#subdirectory=KB_Agent"')


def test_head_commit_is_a_full_sha_or_none(monkeypatch):
    sha = C.head_commit()
    assert sha is None or C._SHA_RE.match(sha)
    monkeypatch.setattr(C, "_git", lambda *a: "not-a-sha")
    assert C.head_commit() is None


@pytest.mark.parametrize("remote", ["git@github.com:Org/repo.git", None])
def test_bundle_gives_the_git_install_and_service_token_flags(tmp_path, monkeypatch, capsys, remote):
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(tmp_path / "tenants.toml"))
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://u:p@localhost:5433/kb_mcp")
    monkeypatch.setenv("KB_GATEWAY_URL", "https://kb-agent.example.com")
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(C, "origin_remote", lambda: remote)
    monkeypatch.setattr(C, "head_commit", lambda: sha)
    assert C.main(["create", "bundle", "--email", "b@example.com", "--no-create-db"]) == 0
    out = capsys.readouterr().out
    assert not BARE_PYPI_INSTALL.search(out)
    assert f"install       = {C.agent_install_command(remote, sha)}\n" in out
    assert f"@{sha}#subdirectory=KB_Agent" in out
    init = next(line for line in out.splitlines() if line.startswith("init "))
    assert "kb-agent init --gateway https://kb-agent.example.com --tenant bundle" in init
    assert "--cf-client-id <" in init and "--cf-client-secret <" in init
    assert " --secret " not in init               # init prompts for it: not in shell history


def test_create_pins_the_owners_emails_into_the_registry(tmp_path, monkeypatch, capsys):
    """With no [owner] emails the env list silently WAS the owner on every surface. The
    first create writes it down (lowercased); later env edits no longer move the owner."""
    reg = tmp_path / "tenants.toml"
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg))
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://u:p@localhost:5433/kb_mcp")
    monkeypatch.setenv("KB_CF_ACCESS_EMAILS", " Me@Example.com , ,me@work.example ")
    monkeypatch.setattr(C, "origin_remote", lambda: None)
    monkeypatch.setattr(C, "head_commit", lambda: None)
    assert C.main(["create", "first", "--email", "f@example.com", "--no-create-db"]) == 0
    data = tomllib.loads(reg.read_text())
    assert data["owner"] == {"emails": ["me@example.com", "me@work.example"]}
    assert "[owner] emails" in capsys.readouterr().out
    monkeypatch.setenv("KB_CF_ACCESS_EMAILS", "someone-else@example.com")
    assert C.main(["create", "second", "--email", "s@example.com", "--no-create-db"]) == 0
    data = tomllib.loads(reg.read_text())
    assert data["owner"]["emails"] == ["me@example.com", "me@work.example"]   # never rewritten
    assert load_registry(reg, env={}).owner.emails == frozenset({"me@example.com",
                                                                 "me@work.example"})


def test_create_with_no_owner_emails_anywhere_says_so(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "tenants.toml"
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg))
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://u:p@localhost:5433/kb_mcp")
    monkeypatch.delenv("KB_CF_ACCESS_EMAILS", raising=False)
    monkeypatch.setattr(C, "origin_remote", lambda: None)
    monkeypatch.setattr(C, "head_commit", lambda: None)
    assert C.main(["create", "first", "--email", "f@example.com", "--no-create-db"]) == 0
    assert "owner" not in tomllib.loads(reg.read_text())
    assert "no owner emails" in capsys.readouterr().err


def test_origin_remote_is_none_outside_a_repository(monkeypatch):
    def boom(*_a, **_k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(C.subprocess, "run", boom)
    assert C.origin_remote() is None


def test_backup_fresh_requires_a_young_file(tmp_path):
    d = tmp_path / "kb_t_alice"
    d.mkdir()
    assert C.backup_fresh(tmp_path, "kb_t_alice") is None
    f = d / "20260101T000000Z.dump.age"
    f.write_bytes(b"x")
    os.utime(f, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    assert C.backup_fresh(tmp_path, "kb_t_alice") is None
    g = d / "20260102T000000Z.dump.age"
    g.write_bytes(b"x")
    assert C.backup_fresh(tmp_path, "kb_t_alice") == g


def test_create_and_delete_provision_a_real_database(tmp_path, monkeypatch, capsys):
    admin = _admin_or_skip()
    reg_path = tmp_path / "tenants.toml"
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg_path))
    monkeypatch.setenv("KB_ADMIN_DATABASE_URL", admin)
    monkeypatch.setenv("KB_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("KB_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.chdir(tmp_path)
    slug = "ztest" + str(int(time.time()))[-6:]
    try:
        assert C.main(["create", slug, "--email", f"{slug}@example.com"]) == 0
        out = capsys.readouterr().out
        assert f"kb_t_{slug}" in out and "agent_secret" in out
        assert not BARE_PYPI_INSTALL.search(out) and "#subdirectory=KB_Agent" in out
        reg = load_registry(reg_path, env=os.environ)
        t = reg.by_slug(slug)
        assert t is not None and t.emails == frozenset({f"{slug}@example.com"})
        assert (tmp_path / "tenants" / slug).is_dir()
        assert oct(reg_path.stat().st_mode & 0o777) == "0o600"
        assert t.db_role == f"kb_t_{slug}" and t.db_password not in out   # never printed
        with psycopg.connect(reg.dsn_for(t), connect_timeout=5) as c:
            n = c.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
            # Migrated AS the tenant's own role: it owns every table it reads and writes.
            assert c.execute("SELECT current_user").fetchone()[0] == f"kb_t_{slug}"
            assert c.execute("SELECT tableowner FROM pg_tables WHERE tablename = 'nodes'"
                             ).fetchone()[0] == f"kb_t_{slug}"
        assert n >= 45                  # a lower bound: later migrations add to the count
        assert C.main(["create", slug, "--email", "x@example.com"]) != 0  # refuses duplicates
        assert C.main(["delete", slug, "--yes"]) != 0                      # no fresh backup
        assert C.main(["delete", slug, "--yes", "--no-backup"]) == 0
        with psycopg.connect(admin, autocommit=True) as c:
            assert c.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                             (f"kb_t_{slug}",)).fetchone() is None
            assert c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                             (f"kb_t_{slug}",)).fetchone() is None          # role dropped too
        assert load_registry(reg_path, env=os.environ).by_slug(slug) is None
    finally:
        _cleanup(admin, f"kb_t_{slug}")


def test_registry_file_is_0600_from_creation_whatever_the_umask(tmp_path, monkeypatch):
    # Path.chmod is disabled so only the mode the file is *created* with counts: the
    # secrets must never sit on disk under the process umask, even briefly.
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    reg_path = tmp_path / "tenants.toml"
    data = {"tenant": {"alice": {"emails": ["a@example.com"], "database": "kb_t_alice",
                                 "agent_secret": "s" * 64, "db_password": "p" * 32,
                                 "enabled": True}}}
    old = os.umask(0o022)
    try:
        C.write_registry_file(reg_path, data)
    finally:
        os.umask(old)
    assert oct(reg_path.stat().st_mode & 0o777) == "0o600"
    assert tomllib.loads(reg_path.read_text()) == data


def test_failed_create_drops_the_new_database(tmp_path, monkeypatch):
    admin = _admin_or_skip()
    reg_path = tmp_path / "tenants.toml"
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg_path))
    monkeypatch.setenv("KB_ADMIN_DATABASE_URL", admin)
    monkeypatch.setenv("KB_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.chdir(tmp_path)
    slug = "zfail" + str(int(time.time()))[-6:]
    created = []
    real_create = C.create_database

    def create_spy(*a, **kw):
        real_create(*a, **kw)
        created.append(a[1])

    def boom(*_a, **_kw):
        raise RuntimeError("registry write failed")

    monkeypatch.setattr(C, "create_database", create_spy)
    monkeypatch.setattr(C, "write_registry_file", boom)
    try:
        with pytest.raises(RuntimeError, match="registry write failed"):
            C.main(["create", slug, "--email", f"{slug}@example.com"])
        assert created == [f"kb_t_{slug}"]              # the database really was created
        with psycopg.connect(admin, autocommit=True) as c:
            assert c.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                             (f"kb_t_{slug}",)).fetchone() is None
            assert c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                             (f"kb_t_{slug}",)).fetchone() is None
        assert not reg_path.exists()
    finally:
        _cleanup(admin, f"kb_t_{slug}")


def test_delete_refuses_an_invalid_registry_entry_without_dropping(tmp_path, monkeypatch):
    # A hand-edited entry naming the owner database must never reach DROP DATABASE.
    reg_path = tmp_path / "tenants.toml"
    reg_path.write_text(C.dump_registry(
        {"tenant": {"x": {"emails": ["x@example.com"], "database": "kb_mcp",
                          "agent_secret": "s" * 64, "db_password": "p" * 32,
                          "enabled": True}}}), encoding="utf-8")
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg_path))
    monkeypatch.setenv("KB_ADMIN_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/postgres")
    dropped = []
    monkeypatch.setattr(C, "drop_database", lambda admin, name: dropped.append(name))
    assert C.main(["delete", "x", "--yes", "--no-backup"]) == 2
    assert dropped == []
    assert "kb_mcp" in reg_path.read_text()             # registry left untouched


def test_rotate_secret_heals_a_registry_whose_tenants_share_a_secret(tmp_path, monkeypatch, capsys):
    """The registry refuses a shared agent_secret and names `kb-tenant rotate-secret` as the
    fix; rotating either tenant must then succeed (it is the one write a broken file allows)."""
    reg_path = tmp_path / "tenants.toml"
    shared = "c" * 64
    reg_path.write_text(f'[tenant.ann]\nemails = ["a@y"]\nagent_secret = "{shared}"\n'
                        f'db_password = "{"p" * 32}"\n'
                        f'[tenant.bob]\nemails = ["b@y"]\nagent_secret = "{shared}"\n'
                        f'db_password = "{"q" * 32}"\n')
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg_path))
    assert C.main(["rotate-secret", "bob"]) == 0
    reg = load_registry(reg_path, env={})
    assert reg.by_slug("ann").agent_secret == shared
    assert reg.by_slug("bob").agent_secret not in (shared, None)


def test_tenant_roles_cannot_reach_each_other_or_the_owner(tmp_path, monkeypatch, capsys):
    """The point of per-tenant roles: a tenant's login opens only its own database. Pairing
    it with another tenant's database, or the owner's, fails at connect; the owner's app
    role cannot open a tenant database either."""
    admin = _admin_or_skip()
    reg_path = tmp_path / "tenants.toml"
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg_path))
    monkeypatch.setenv("KB_ADMIN_DATABASE_URL", admin)
    monkeypatch.setenv("KB_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("KB_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.chdir(tmp_path)
    stamp = str(int(time.time()))[-6:]
    a, b = "zroa" + stamp, "zrob" + stamp
    try:
        for slug in (a, b):
            assert C.main(["create", slug, "--email", f"{slug}@example.com"]) == 0
        reg = load_registry(reg_path, env=os.environ)
        ta, tb = reg.by_slug(a), reg.by_slug(b)
        owner_db = TEST_DATABASE_URL.rsplit("/", 1)[1].split("?")[0]

        def login(t, database):
            return reg.dsn_for(t).rsplit("/", 1)[0] + "/" + database

        with psycopg.connect(reg.dsn_for(ta), connect_timeout=5) as c:
            assert c.execute("SELECT current_database()").fetchone()[0] == f"kb_t_{a}"
        for dsn in (login(ta, f"kb_t_{b}"), login(tb, f"kb_t_{a}"), login(ta, owner_db)):
            with pytest.raises(psycopg.OperationalError, match="permission denied"):
                psycopg.connect(dsn, connect_timeout=5).close()
        owner_login = TEST_DATABASE_URL.rsplit("/", 1)[0]
        with psycopg.connect(admin, autocommit=True) as c:
            owner_is_super = c.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname = %s",
                (psycopg.conninfo.conninfo_to_dict(TEST_DATABASE_URL)["user"],)).fetchone()[0]
        if not owner_is_super:          # superusers bypass CONNECT; CI's test role is one
            with pytest.raises(psycopg.OperationalError, match="permission denied"):
                psycopg.connect(f"{owner_login}/kb_t_{a}", connect_timeout=5).close()
        # The owner's own database stays open to its app role.
        with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as c:
            c.execute("SELECT 1")
        # rotate-db-password: the old password stops working, the new one works.
        old = reg.dsn_for(ta)
        assert C.main(["rotate-db-password", a]) == 0
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(old, connect_timeout=5).close()
        fresh = load_registry(reg_path, env=os.environ)
        with psycopg.connect(fresh.dsn_for(fresh.by_slug(a)), connect_timeout=5) as c:
            c.execute("SELECT 1")
    finally:
        _cleanup(admin, f"kb_t_{a}")
        _cleanup(admin, f"kb_t_{b}")


@pytest.mark.parametrize("name", ["kb_mcp", "kb_app", "postgres", "kb_t_", "kb_t_Bad", "x_kb_t_a"])
def test_only_kb_t_names_can_be_dropped_or_provisioned(name):
    """A bad argument must never reach DROP DATABASE / DROP ROLE for the owner's objects;
    the guard runs before any connection is attempted."""
    nowhere = "postgresql://nobody@127.0.0.1:1/postgres"
    for fn in (lambda: C.drop_database(nowhere, name),
               lambda: C.create_database(nowhere, name, password="p" * 32),
               lambda: C.set_role_password(nowhere, name, "p" * 32)):
        with pytest.raises(SystemExit, match="kb_t_<slug>"):
            fn()
