import time
from pathlib import Path

import pytest

from kb_core import tenants as T


GOOD = '''
[owner]
emails = ["Owner@Example.com"]
database = "kb_mcp"

[tenant.alice]
emails = ["alice@example.com", "ALICE@work.example"]
database = "kb_t_alice"
agent_secret = "a" * 64
db_password = "papapapapapapapapapapapapapapapapapapapa"
vault_layer = "Obsidian"
policy = "tenants/alice/policy.toml"
enabled = true

[tenant.bob]
emails = ["bob@example.com"]
database = "kb_t_bob"
agent_secret = "b" * 64
db_password = "pbpbpbpbpbpbpbpbpbpbpbpbpbpbpbpbpbpbpbpb"
enabled = false
'''.replace('"a" * 64', '"' + "a" * 64 + '"').replace('"b" * 64', '"' + "b" * 64 + '"')


def _write(tmp_path: Path, text: str = GOOD) -> Path:
    p = tmp_path / "tenants.toml"
    p.write_text(text)
    return p


def test_load_registry_parses_owner_and_tenants(tmp_path):
    reg = T.load_registry(_write(tmp_path), env={})
    assert reg.owner.slug == "owner" and reg.owner.database == "kb_mcp"
    assert reg.owner.emails == frozenset({"owner@example.com"})
    alice = reg.by_slug("alice")
    assert alice.emails == frozenset({"alice@example.com", "alice@work.example"})
    assert alice.policy_path == (tmp_path / "tenants/alice/policy.toml").resolve()
    assert alice.agent_secret == "a" * 64 and alice.vault_layer == "Obsidian"


def test_by_email_is_case_insensitive_and_skips_disabled(tmp_path):
    reg = T.load_registry(_write(tmp_path), env={})
    assert reg.by_email("Alice@Work.Example").slug == "alice"
    assert reg.by_email("owner@example.com").is_owner
    assert reg.by_email("bob@example.com") is None          # disabled
    assert reg.by_slug("bob").enabled is False               # but still addressable
    assert [t.slug for t in reg.tenants()] == ["owner", "alice"]
    assert [t.slug for t in reg.tenants(include_disabled=True)] == ["owner", "alice", "bob"]


def test_missing_file_gives_owner_only_registry(tmp_path):
    reg = T.load_registry(tmp_path / "absent.toml",
                          env={"KB_CF_ACCESS_EMAILS": "me@example.com", "POSTGRES_DB": "kb_x"})
    assert reg.owner.database == "kb_x" and reg.owner.emails == frozenset({"me@example.com"})
    assert reg.tenants(include_owner=False) == []


@pytest.mark.parametrize("bad, msg", [
    ('[tenant.Bad]\nemails=["x@y"]\nagent_secret="' + "a" * 64 + '"\ndb_password="' + "p" * 32 + '"\n', "slug"),
    ('[tenant.owner]\nemails=["x@y"]\nagent_secret="' + "a" * 64 + '"\ndb_password="' + "p" * 32 + '"\n', "slug"),
    ('[tenant.ann]\nemails=["x@y"]\ndatabase="other"\nagent_secret="' + "a" * 64 + '"\ndb_password="' + "p" * 32 + '"\n', "database"),
    ('[tenant.ann]\nemails=["x@y"]\nagent_secret="short"\n', "agent_secret"),
    ('[tenant.ann]\nemails=[]\nagent_secret="' + "a" * 64 + '"\ndb_password="' + "p" * 32 + '"\n', "emails"),
    ('[tenant.ann]\nemails=["x@y"]\nagent_secret="' + "a" * 64 + '"\ndb_password="' + "p" * 32 + '"\nbogus=1\n', "unknown key"),
    ('[owner]\nemails=["x@y"]\n[tenant.ann]\nemails=["x@y"]\nagent_secret="' + "a" * 64 + '"\ndb_password="' + "p" * 32 + '"\n', "already belongs"),
    ('[tenant.ann]\nemails=["x@y"]\nagent_secret="' + "a" * 64 + '"\ndb_password="' + "p" * 32 + '"\nenabled="false"\n', "enabled"),
    ('[tenant.ann]\nemails=["x@y"]\nagent_secret="' + "a" * 64 + '"\ndb_password="' + "p" * 32 + '"\nvault_layer=1\n', "vault_layer"),
    ('[tenant.ann]\nemails=["x@y"]\nagent_secret="' + "a" * 64 + '"\ndb_password="' + "p" * 32 + '"\npolicy=1\n', "policy"),
    ('[owner]\nprompts_overlay=["p"]\n', "prompts_overlay"),
])
def test_validation_is_loud(tmp_path, bad, msg):
    with pytest.raises(T.RegistryError, match=msg):
        T.load_registry(_write(tmp_path, bad), env={})


def test_two_tenants_sharing_an_agent_secret_are_refused(tmp_path):
    """The gateway picks the secret by X-KB-Tenant, so a shared secret would let one
    tenant's agent sign as the other. Disabled tenants count (re-enabling one must not
    silently create the collision), and the error never echoes the secret."""
    shared = "c" * 64
    toml = (f'[tenant.ann]\nemails=["a@y"]\nagent_secret="{shared}"\ndb_password="{"p" * 32}"\n'
            f'[tenant.bob]\nemails=["b@y"]\nagent_secret="{shared}"\ndb_password="{"q" * 32}"\nenabled=false\n')
    with pytest.raises(T.RegistryError, match=r"\[tenant.bob\]: agent_secret is already used by "
                                              r"\[tenant.ann\]") as exc:
        T.load_registry(_write(tmp_path, toml), env={})
    assert shared not in str(exc.value)


def test_owner_emails_string_is_rejected_not_split(tmp_path):
    with pytest.raises(T.RegistryError, match=r"\[owner\]: emails"):
        T.load_registry(_write(tmp_path, '[owner]\nemails = "me@example.com"\n'), env={})


@pytest.mark.parametrize("bad, key", [
    ("owner = 1\n", "owner"),
    ("tenant = 2\n", "tenant"),
    ("tenant.ann = 3\n", r"\[tenant\.ann\]"),
])
def test_non_table_blocks_raise_registry_error(tmp_path, bad, key):
    with pytest.raises(T.RegistryError, match=key + r".*must be a table"):
        T.load_registry(_write(tmp_path, bad), env={})


def test_dsn_for_derives_tenant_db_from_KB_DATABASE_URL(tmp_path):
    reg = T.load_registry(_write(tmp_path), env={"KB_DATABASE_URL": "postgresql://u:p@localhost:5433/kb_mcp_test"})
    assert reg.dsn_for(reg.owner) == "postgresql://u:p@localhost:5433/kb_mcp_test"
    # A tenant connects as its OWN role, never with the owner's credentials.
    assert reg.dsn_for(reg.by_slug("alice")) == (
        "postgresql://kb_t_alice:" + "pa" * 20 + "@localhost:5433/kb_t_alice")


def test_dsn_for_keeps_host_port_and_query_but_swaps_the_login(tmp_path):
    reg = T.load_registry(_write(tmp_path), env={
        "KB_DATABASE_URL": "postgresql://kb_app:secret@db.internal:6543/kb_mcp?sslmode=require"})
    assert reg.dsn_for(reg.by_slug("bob")) == (
        "postgresql://kb_t_bob:" + "pb" * 20 + "@db.internal:6543/kb_t_bob?sslmode=require")


def test_dsn_for_refuses_a_tenant_without_its_own_login():
    owner = T.Tenant("owner", frozenset({"o@x"}), database="kb_mcp")
    bare = T.Tenant("t", frozenset({"t@x"}), database="kb_t_t", agent_secret="s" * 64)
    reg = T.Registry(owner, {"t": bare}, env={"KB_DATABASE_URL": "postgresql://u:p@h:1/kb_mcp"})
    with pytest.raises(T.RegistryError, match="owner's credentials"):
        reg.dsn_for(bare)


@pytest.mark.parametrize("pw, ok", [(None, False), ("short", False), ("p" * 31, False),
                                    ("p@ss" * 10, False), ("p" * 32, True), ("A1" * 64, True)])
def test_db_password_is_required_and_dsn_safe(tmp_path, pw, ok):
    line = f'db_password="{pw}"\n' if pw is not None else ""
    toml = f'[tenant.ann]\nemails=["a@y"]\nagent_secret="{"s" * 64}"\n{line}'
    if ok:
        assert T.load_registry(_write(tmp_path, toml), env={}).by_slug("ann").db_role == "kb_t_ann"
    else:
        with pytest.raises(T.RegistryError, match="db_password"):
            T.load_registry(_write(tmp_path, toml), env={})


def test_dsn_for_without_KB_DATABASE_URL_matches_kb_svc(tmp_path):
    reg = T.load_registry(_write(tmp_path), env={"KB_APP_USER": "kb_app", "KB_APP_PASSWORD": "pw", "DB_HOST_PORT": "5433"})
    assert reg.dsn_for(reg.owner) == "postgresql://kb_app:pw@localhost:5433/kb_mcp"
    assert reg.dsn_for(reg.by_slug("alice")) == (
        "postgresql://kb_t_alice:" + "pa" * 20 + "@localhost:5433/kb_t_alice")


def test_reload_picks_up_changes_and_keeps_last_good_on_error(tmp_path, monkeypatch):
    p = _write(tmp_path)
    reg = T.load_registry(p, env={})
    monkeypatch.setattr(T, "_RELOAD_CHECK_S", 0.0)
    p.write_text(GOOD.replace('enabled = false', 'enabled = true'))
    time.sleep(0.01)
    assert reg.by_email("bob@example.com").slug == "bob"
    p.write_text("this is = not [valid")
    time.sleep(0.01)
    assert reg.by_email("bob@example.com").slug == "bob"      # previous registry kept


def test_unreadable_file_is_registry_error_and_reload_keeps_last_good(tmp_path, monkeypatch):
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"[owner]\nemails = [\"\xff\xfe@example.com\"]\n")
    with pytest.raises(T.RegistryError, match="bad.toml"):
        T.load_registry(bad, env={})
    p = _write(tmp_path)
    reg = T.load_registry(p, env={})
    monkeypatch.setattr(T, "_RELOAD_CHECK_S", 0.0)
    p.write_bytes(GOOD.encode() + b"\n# \xff\xfe not utf-8\n")
    time.sleep(0.01)
    assert reg.by_email("alice@example.com").slug == "alice"  # previous registry kept
    assert reg.by_email("bob@example.com") is None


def test_cli_env_prints_shell_lines(tmp_path, capsys, monkeypatch):
    p = _write(tmp_path)
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(p))
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://u:p@localhost:5433/kb_mcp")
    assert T.main(["env", "alice"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert f"KB_DATABASE_URL=postgresql://kb_t_alice:{'pa' * 20}@localhost:5433/kb_t_alice" in out
    assert "KB_VAULT_LAYER=Obsidian" in out
    assert any(line.startswith("KB_POLICY_CONFIG=") for line in out)
    assert T.main(["dsn", "nobody"]) == 2


def test_registry_path_defaults_to_the_kb_core_root_not_the_cwd(tmp_path, monkeypatch):
    """KB_MCP runs from ../KB_MCP; a cwd-relative default would read a file that never
    exists there, so MCP would refuse every tenant. The default is the KB_Core root's file."""
    core = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tenants.toml").write_text("not = 'this one'\n", encoding="utf-8")
    assert T.core_root() == core
    assert T.registry_path({}) == core / "tenants.toml"
    assert T.registry_path({"KB_TENANTS_CONFIG": "/abs/t.toml"}) == Path("/abs/t.toml")
    assert T.load_registry(env={}).path == core / "tenants.toml"


def test_library_tokens_map_a_device_to_its_tenant_and_must_be_unique(tmp_path):
    text = (GOOD.replace('database = "kb_mcp"\n',
                         'database = "kb_mcp"\nlibrary_tokens = ["dev-a.access"]\n', 1)
                .replace('vault_layer = "Obsidian"\n',
                         'vault_layer = "Obsidian"\nlibrary_tokens = ["dev-b.access"]\n', 1))
    reg = T.load_registry(_write(tmp_path, text), env={})
    assert reg.by_library_token("dev-a.access").is_owner
    assert reg.by_library_token("dev-b.access").slug == "alice"
    assert reg.by_library_token("dev-z.access") is None
    assert reg.by_library_token("") is None
    # disabled tenants count: re-enabling bob must not make one device two tenants
    dup = text.replace('database = "kb_t_bob"\n',
                       'database = "kb_t_bob"\nlibrary_tokens = ["dev-a.access"]\n', 1)
    with pytest.raises(T.RegistryError, match="library token"):
        T.load_registry(_write(tmp_path, dup), env={})
    for bad in ('"dev-a.access"', '[""]', "[1]"):
        broken = GOOD.replace('database = "kb_mcp"\n',
                              f'database = "kb_mcp"\nlibrary_tokens = {bad}\n', 1)
        with pytest.raises(T.RegistryError, match="library_tokens"):
            T.load_registry(_write(tmp_path, broken), env={})
