"""Tenant daemons never load the owner's policy.

A tenant daemon runs with cwd = KB_Core, where the owner's policy.toml and prompts.local/
overlay live. Each daemon main() must build its KnowledgeBase with the tenant's registry
policy (tenancy.policy_for), and load_policy() must refuse the ./policy.toml fallback when
KB_TENANT names a tenant. Pure — no DB: the daemons' pool/migrate/KnowledgeBase are faked.
"""
import importlib
from pathlib import Path

import pytest

from kb_core import tenants as T
from kb_core.policy import DEFAULT_POLICY, PolicyError, load_policy

SECRET = "s" * 64

OWNER_POLICY = """
[[decay]]
name = "owner-private"
match = { tags = ["owner-secret"] }
half_life_days = 3
floor = 0.1

[taxonomy]
categories = [{ name = "owner thing", kind = "concept", description = "owner only" }]

[prompts]
overlay_dir = "prompts.local"
"""

DAEMONS = [
    "kb_core.ingest.daemon",
    "kb_core.analysis.daemon",
    "kb_core.entity_resolve_daemon",
    "kb_core.consolidate_daemon",
]


def _owner_checkout(tmp_path):
    """cwd as a real install has it: the owner's policy.toml + a prompts.local/ overlay."""
    (tmp_path / "policy.toml").write_text(OWNER_POLICY, encoding="utf-8")
    overlay = tmp_path / "prompts.local"
    overlay.mkdir()
    (overlay / "entities_examples.md").write_text("OWNER-PRIVATE-EXAMPLE", encoding="utf-8")
    reg = tmp_path / "tenants.toml"
    reg.write_text(f'[tenant.alice]\nemails = ["alice@example.com"]\n'
                   f'agent_secret = "{SECRET}"\ndb_password = "{"p" * 32}"\n'
                   f'[tenant.carol]\nemails = ["carol@example.com"]\n'
                   f'agent_secret = "{"c" * 64}"\ndb_password = "{"q" * 32}"\n'
                   'policy = "carol-policy.toml"\n',
                   encoding="utf-8")
    (tmp_path / "carol-policy.toml").write_text(
        "[importance]\nrecency_floor = 0.5\n[prompts]\noverlay = false\n", encoding="utf-8")
    return reg


def test_load_policy_refuses_the_cwd_fallback_for_a_tenant(tmp_path, monkeypatch):
    _owner_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PolicyError, match="KB_TENANT"):
        load_policy(env={"KB_TENANT": "alice"})
    # Owner mode is unchanged: unset or "owner" still reads ./policy.toml.
    assert load_policy(env={}).decay[0].name == "owner-private"
    assert load_policy(env={"KB_TENANT": "owner"}).decay[0].name == "owner-private"
    # An explicit file is still honoured in tenant mode (kb-svc exports one).
    explicit = load_policy(env={"KB_TENANT": "alice",
                                "KB_POLICY_CONFIG": str(T.tenant_neutral_policy_path())})
    assert explicit.to_dict() == DEFAULT_POLICY.to_dict()


def test_shipped_tenant_policy_is_inert_defaults_with_no_overlay():
    path = T.tenant_neutral_policy_path()
    assert path.is_absolute() and path.is_file() and path.name == "policy.tenant.toml"
    pol = load_policy(env={}, path=str(path))
    assert pol.to_dict() == DEFAULT_POLICY.to_dict()
    assert pol.prompts.overlay_dir is None


def test_prompts_overlay_false_means_no_overlay(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text("[prompts]\noverlay = false\n", encoding="utf-8")
    assert load_policy(env={}, path=str(p)).prompts.overlay_dir is None
    p.write_text('[prompts]\noverlay = false\noverlay_dir = "x"\n', encoding="utf-8")
    with pytest.raises(PolicyError, match="overlay"):
        load_policy(env={}, path=str(p))
    p.write_text("[prompts]\noverlay = true\n", encoding="utf-8")
    assert load_policy(env={}, path=str(p)).prompts.overlay_dir == "prompts.local"


def test_cli_env_points_a_tenant_without_policy_at_the_neutral_file(tmp_path, monkeypatch,
                                                                    capsys):
    reg = _owner_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg))
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://u:p@localhost:5433/kb_mcp")
    assert T.main(["env", "alice"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert f"KB_POLICY_CONFIG={T.tenant_neutral_policy_path()}" in out
    assert T.main(["env", "carol"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert f"KB_POLICY_CONFIG={(tmp_path / 'carol-policy.toml').resolve()}" in out


class _Built(Exception):
    pass


@pytest.mark.parametrize("module", DAEMONS)
@pytest.mark.parametrize("slug", ["alice", "carol"])
def test_every_tenant_daemon_builds_the_registry_policy(module, slug, tmp_path, monkeypatch):
    reg = _owner_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KB_TENANT", slug)
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg))
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://u:p@localhost:1/kb_t_" + slug)
    monkeypatch.setenv("KB_INGEST_RECEIVER", "0")   # as kb-svc.sh runs tenant daemons
    monkeypatch.delenv("KB_POLICY_CONFIG", raising=False)
    monkeypatch.setattr("sys.argv", ["daemon"])
    mod = importlib.import_module(module)
    built: dict = {}

    def fake_kb(pool, registry, *args, **kwargs):
        built["policy"] = kwargs.get("policy")
        raise _Built

    monkeypatch.setattr(mod, "open_pool", lambda *a, **k: object())
    monkeypatch.setattr(mod, "migrate_pool", lambda pool: None)
    monkeypatch.setattr(mod, "KnowledgeBase", fake_kb)
    with pytest.raises(_Built):
        mod.main()
    pol = built["policy"]
    assert pol is not None, f"{module} built its KnowledgeBase with no policy"
    assert pol.prompts.overlay_dir is None
    assert all(r.name != "owner-private" for r in pol.decay)
    if slug == "alice":                       # no registry policy: the shipped defaults
        assert pol == DEFAULT_POLICY
    else:                                     # the registry's own file, never ./policy.toml
        assert pol.importance.recency_floor == 0.5
        assert pol.source_path == str((tmp_path / "carol-policy.toml").resolve())


def _tenant_with_policy(tmp_path, slug: str, policy_text: str):
    """A registry tenant whose own policy file lives in tenants/<slug>/, next to the owner
    checkout (cwd) that holds policy.toml + prompts.local/."""
    reg = _owner_checkout(tmp_path)
    d = tmp_path / "tenants" / slug
    d.mkdir(parents=True)
    (d / "policy.toml").write_text(policy_text, encoding="utf-8")
    reg.write_text(reg.read_text(encoding="utf-8")
                   + f'[tenant.{slug}]\nemails = ["{slug}@example.com"]\n'
                     f'agent_secret = "{slug[0] * 64}"\ndb_password = "{"r" * 32}"\n'
                     f'policy = "tenants/{slug}/policy.toml"\n',
                   encoding="utf-8")
    return reg, d


@pytest.mark.parametrize("text", [
    "[importance]\nrecency_floor = 0.4\n",                            # no [prompts] at all
    (Path(__file__).resolve().parents[1]
     / "policy.toml.example").read_text(encoding="utf-8")
    .replace('overlay_dir = "prompts.local"', ""),                    # example, section kept
])
def test_a_tenant_policy_file_never_reaches_the_owners_overlay(tmp_path, monkeypatch, text):
    """A tenant's own policy file with no overlay setting must not default
    to the cwd-relative prompts.local -- the OWNER's overlay. For a tenant it means none (or,
    with [prompts] present, the directory beside the tenant's own file)."""
    from kb_core import tenancy
    from kb_core.prompts import PromptResolver, prompts_base_dir
    reg, d = _tenant_with_policy(tmp_path, "dave", text)
    monkeypatch.chdir(tmp_path)
    env = {"KB_TENANTS_CONFIG": str(reg), "KB_TENANT": "dave"}
    owner_file = (tmp_path / "prompts.local" / "entities_examples.md").resolve()
    tenant = T.load_registry(env=env).by_slug("dave")
    for pol in (tenancy.policy_for(tenant, env), tenancy.daemon_policy(env)):
        od = pol.prompts.overlay_dir
        assert od is None or (Path(od).resolve().parent == d.resolve())
        hit = PromptResolver(pol, prompts_base_dir()).path_for("entities_examples.md")
        assert hit.resolve() != owner_file


def test_a_tenant_relative_overlay_dir_resolves_beside_its_own_file(tmp_path, monkeypatch):
    from kb_core import tenancy
    from kb_core.prompts import PromptResolver, prompts_base_dir
    reg, d = _tenant_with_policy(tmp_path, "erin", '[prompts]\noverlay_dir = "prompts.local"\n')
    (d / "prompts.local").mkdir()
    (d / "prompts.local" / "entities_examples.md").write_text("ERIN", encoding="utf-8")
    monkeypatch.chdir(tmp_path)                       # the owner's prompts.local is in cwd too
    env = {"KB_TENANTS_CONFIG": str(reg), "KB_TENANT": "erin"}
    tenant = T.load_registry(env=env).by_slug("erin")
    for pol in (tenancy.policy_for(tenant, env), tenancy.daemon_policy(env)):
        assert pol.prompts.overlay_dir == str((d / "prompts.local").resolve())
        hit = PromptResolver(pol, prompts_base_dir()).path_for("entities_examples.md")
        assert hit.read_text(encoding="utf-8") == "ERIN"


def test_the_owner_policy_overlay_is_unchanged(tmp_path, monkeypatch):
    """Owner path byte for byte: ./policy.toml with its cwd-relative prompts.local."""
    from kb_core import tenancy
    reg = _owner_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    env = {"KB_TENANTS_CONFIG": str(reg)}
    owner = T.load_registry(env=env).owner
    assert tenancy.policy_for(owner, env).prompts.overlay_dir == "prompts.local"
    assert load_policy(env={}, path=str(tmp_path / "policy.toml")).prompts.overlay_dir \
        == "prompts.local"


@pytest.mark.parametrize("module", DAEMONS)
def test_an_unknown_tenant_slug_fails_loud_before_building(module, tmp_path, monkeypatch):
    reg = _owner_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KB_TENANT", "nobody")
    monkeypatch.setenv("KB_TENANTS_CONFIG", str(reg))
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://u:p@localhost:1/kb_t_nobody")
    monkeypatch.setenv("KB_INGEST_RECEIVER", "0")   # as kb-svc.sh runs tenant daemons
    monkeypatch.setattr("sys.argv", ["daemon"])
    mod = importlib.import_module(module)
    monkeypatch.setattr(mod, "open_pool", lambda *a, **k: object())
    monkeypatch.setattr(mod, "migrate_pool", lambda pool: None)
    monkeypatch.setattr(mod, "KnowledgeBase",
                        lambda *a, **k: pytest.fail("built a KnowledgeBase for an unknown tenant"))
    with pytest.raises(T.RegistryError, match="nobody"):
        mod.main()
