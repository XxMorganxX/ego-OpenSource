"""Static contracts for the launchd-managed KB service stack."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_kb_dev_supervises_concept_consolidation():
    script = (ROOT / "scripts" / "kb-dev.sh").read_text(encoding="utf-8")

    services = next(line for line in script.splitlines() if line.startswith("SERVICES=("))
    assert "consolidate" in services
    assert "consolidate →" in script


def test_kb_svc_never_defaults_the_ingest_hmac_secret():
    """The receiver verifies X-KB-Signature correctly (constant-time HMAC-SHA256), so
    the only way in is a known key — and kb-svc.sh used to supply one, defaulting
    KB_INGEST_SECRET to the literal "dev-secret" and silently defeating daemon.py's
    os.environ[...] requirement. Anyone who read the repo could forge an ingest event.
    Start must fail loudly when it is unset."""
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")

    # match the shell default-value syntax, not the bare word — the comment
    # explaining this history legitimately names the old value.
    assert ":-dev-secret" not in script
    block = script.split("ingest)", 1)[1].split(";;", 1)[0]
    assert 'KB_INGEST_SECRET="${KB_INGEST_SECRET:-' not in block, \
        "no shell default for the HMAC key"
    assert 'if [[ -z "${KB_INGEST_SECRET:-}" ]]' in block
    assert "exit 1" in block


def test_kb_svc_runs_services_as_the_non_superuser_app_role():
    """Runtime must not connect as `kb`: it is the cluster's bootstrap superuser and
    Postgres refuses to demote it, so privilege separation depends entirely on the DSN
    being built from the app role."""
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")

    assert 'PGUSER="${KB_APP_USER:-' in script
    assert 'PGPASS="${KB_APP_PASSWORD:-' in script


def test_kb_svc_runs_the_consolidation_daemon_with_db_and_inference():
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")

    assert "consolidate)" in script
    assert "kb-mcp-consolidated" in script
    block = script.split("consolidate)", 1)[1].split(";;", 1)[0]
    assert "wait_for_db" in block
    assert 'KB_DATABASE_URL="$DBURL"' in block
    assert 'KB_INFERENCE_URL="http://127.0.0.1:$INFERENCE_PORT"' in block


def test_kb_svc_exposes_quiesced_cursor_adoption_command():
    """Rollout must migrate/adopt while the long-running writers remain stopped."""
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")

    assert "adopt-cursors)" in script
    block = script.split("adopt-cursors)", 1)[1].split(";;", 1)[0]
    assert "wait_for_db" in block
    assert 'KB_DATABASE_URL="$DBURL"' in block
    assert "kb-mcp-reanalyzed --adopt-only" in block


def test_kb_svc_mcp_public_requires_an_auth_mode():
    """The public listener is the Cloudflare tunnel's only allowed origin; starting
    it without an auth mode would expose the bare tool surface. Same loud-failure
    contract as the ingest HMAC secret."""
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")

    block = script.split("mcp-public)", 1)[1].split(";;", 1)[0]
    assert 'if [[ -z "${KB_MCP_PUBLIC_AUTH:-}" ]]' in block
    assert "exit 1" in block
    assert 'KB_MCP_AUTH="$KB_MCP_PUBLIC_AUTH"' in block
    assert 'MCP_PUBLIC_PORT="${KB_MCP_PUBLIC_PORT:-3100}"' in script


def test_kb_svc_local_mcp_never_inherits_the_public_auth_mode():
    """kb-svc.sh sources .env with set -a. The durable var is KB_MCP_PUBLIC_AUTH
    (not KB_MCP_AUTH) precisely so the LOCAL listener can't accidentally start
    demanding tokens from local clients — and the local case unsets the process
    var as a belt against future .env drift."""
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")

    local_block = script.split("\n  mcp)", 1)[1].split(";;", 1)[0]
    assert "unset KB_MCP_AUTH" in local_block


def test_kb_svc_local_mcp_never_inherits_the_public_hostname():
    """KB_MCP_PUBLIC_HOSTNAME lives in .env for the public listener. If the LOCAL
    (unauthenticated) listener inherited it, FastMCP would accept the tunnel's Host
    header there, so a tunnel aimed at the wrong port would serve the bare tool
    surface publicly. The local case unsets it next to KB_MCP_AUTH, and the comment
    on its port must not point a tunnel at it."""
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")

    local_block = script.split("\n  mcp)", 1)[1].split(";;", 1)[0]
    assert "unset KB_MCP_PUBLIC_HOSTNAME" in local_block
    assert 'KB_MCP_PUBLIC_HOSTNAME=' not in local_block
    port_line = next(ln for ln in script.splitlines() if ln.startswith("MCP_PORT="))
    assert "tunnel.sh" not in port_line


def test_kb_svc_dry_run_local_mcp_drops_the_public_hostname_and_auth(tmp_path):
    """Behavioural half: with both vars in .env (sourced with set -a), the local
    listener starts without either, while mcp-public still receives the hostname."""
    repo = _fixture_repo(tmp_path)
    with (repo / ".env").open("a", encoding="utf-8") as f:
        f.write("KB_MCP_PUBLIC_HOSTNAME=kb-mcp.example.com\n"
                "KB_MCP_AUTH=internal-secret\n"
                "KB_MCP_PUBLIC_AUTH=internal-secret\n")
    local = _env_lines(_dry_run(repo, "mcp").stdout)
    assert "KB_MCP_PUBLIC_HOSTNAME" not in local
    assert "KB_MCP_AUTH" not in local
    assert local["KB_MCP_PORT"] == "3000"
    public = _env_lines(_dry_run(repo, "mcp-public").stdout)
    assert public["KB_MCP_PUBLIC_HOSTNAME"] == "kb-mcp.example.com"
    assert public["KB_MCP_AUTH"] == "internal-secret"
    assert public["KB_MCP_PORT"] == "3100"


def test_kb_dev_supervises_the_public_mcp_listener():
    script = (ROOT / "scripts" / "kb-dev.sh").read_text(encoding="utf-8")

    services = next(line for line in script.splitlines() if line.startswith("SERVICES=("))
    assert "mcp-public" in services
    assert "mcp-public →" in script


def test_kb_svc_mcp_public_sets_the_tunnel_hostname_for_transport_security():
    """FastMCP auto-enables DNS-rebinding protection on loopback binds with a
    localhost-only Host allowlist; tunnel traffic carries the public Host and was
    421'd AFTER passing the auth wall. The service must
    tell the server its public hostname — and, since the hostname is personal to
    whoever's tunnel it is, kb-svc.sh must not default it (mirroring the
    KB_MCP_PUBLIC_AUTH gate): it must refuse to start mcp-public without one."""
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")

    block = script.split("mcp-public)", 1)[1].split(";;", 1)[0]
    assert 'KB_MCP_PUBLIC_HOSTNAME="$KB_MCP_PUBLIC_HOSTNAME"' in block
    # Shape check, not a literal-string check: a bash `${VAR:-}` empty-default is the
    # legitimate unset-test idiom (used just above to guard the refusal), but
    # `${KB_MCP_PUBLIC_HOSTNAME:-<anything nonempty>}` would silently resurrect a
    # hardcoded personal hostname default -- catch that shape regardless of what
    # literal value a future edit might reintroduce.
    assert not re.search(r"\$\{KB_MCP_PUBLIC_HOSTNAME:-[^}]+\}", block)
    assert "refusing to start mcp-public: KB_MCP_PUBLIC_HOSTNAME" in block


def test_kb_dev_label_uses_the_service_prefix_env_var():
    """Launchd labels were previously hardcoded to a personal namespace; label()
    must derive the prefix from KB_SERVICE_PREFIX (defaulting to com.kb) so a fresh
    clone gets a working, non-colliding namespace without editing the script."""
    script = (ROOT / "scripts" / "kb-dev.sh").read_text(encoding="utf-8")
    assert '${KB_SERVICE_PREFIX:-com.kb}' in script


def test_roles_init_script_contract():
    """Nothing in the repo used to create kb_app/kb_test on a fresh clone — they were
    hand-created on the owner's machine, so a stranger's first `docker compose up`
    would fail DB auth undiagnosably. 02-roles.sh runs once via
    docker-entrypoint-initdb.d on an empty volume and provisions both roles plus the
    kb_mcp_test test database; docker-compose.yml must pass the env through."""
    text = (ROOT / "db" / "init" / "02-roles.sh").read_text(encoding="utf-8")
    assert "CREATE ROLE" in text
    assert "KB_APP_USER" in text and "KB_APP_PASSWORD" in text
    assert "kb_mcp_test" in text
    # The tenant-isolation tests' second database: kb_test can't create the vector
    # extension itself, so first init provisions it (with extensions) as well.
    assert "kb_mcp_test_b" in text
    # Pin the actual privilege contract, not just presence of the role names — a
    # future loosening (e.g. dropping NOCREATEDB, or granting kb_app CREATEROLE)
    # must fail this test, not just be catchable by re-reading the script.
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE" in text, \
        "kb_app must stay a plain, unprivileged LOGIN role"
    # Space-before-CREATEDB (not "NOCREATEDB") so this can't be satisfied by
    # substring-matching kb_app's own NOCREATEDB above.
    assert "NOSUPERUSER CREATEDB NOCREATEROLE" in text, \
        "kb_test must be able to create its own scratch/test DBs"
    assert "OWNER TO" in text, "kb_app must own kb_mcp (ALTER DATABASE ... OWNER TO)"
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "KB_APP_USER" in compose and "KB_TEST_PASSWORD" in compose


def test_kb_dev_pulls_required_ollama_models():
    """Nothing used to pull the Ollama models during bootstrap — a fresh machine
    reached "ollama is up" (the /api/tags poll only checks the daemon is reachable,
    not that any model is loaded) with a permanently unhealthy inference service.
    ensure_ollama() must also pull both the configured chat model and the fixed
    embedding model when either is missing."""
    script = (ROOT / "scripts" / "kb-dev.sh").read_text(encoding="utf-8")
    block = script.split("ensure_ollama()", 1)[1].split("\n}\n", 1)[0]
    assert "ollama pull" in block
    assert "OLLAMA_MODEL" in block
    assert "qwen3-embedding:0.6b" in block


SECRET_PW = "owner-db-pw-do-not-print"
TENANT_DB_PW = "tenantdbpwdonotprint" * 2
TENANT_SECRET = "t" * 64


def _fixture_repo(tmp_path: Path) -> Path:
    """A KB_Core stand-in: the owner's .env and policy.toml, a one-tenant registry, and the
    real venv (so `python -m kb_core.tenants env` runs the code under test)."""
    repo = tmp_path / "KB_Core"
    repo.mkdir()
    (repo / ".venv").symlink_to(ROOT / ".venv")
    (repo / ".env").write_text(
        "KB_APP_USER=kb_app\n"
        f"KB_APP_PASSWORD={SECRET_PW}\n"
        f"KB_DATABASE_URL=postgresql://kb_app:{SECRET_PW}@localhost:5433/kb_mcp\n"
        f"KB_POLICY_CONFIG={repo}/policy.toml\n"
        "KB_ENRICH_CONCURRENCY=8\n"
        "KB_OBSIDIAN_VAULT=/owner/vault\n"
        "KB_VAULT_LAYER=Owner\n"
        "KB_INGEST_SECRET=owner-hmac\n",
        encoding="utf-8")
    (repo / "policy.toml").write_text('[prompts]\noverlay_dir = "prompts.local"\n',
                                      encoding="utf-8")
    (repo / "tenants.toml").write_text(
        f'[tenant.alice]\nemails = ["alice@example.com"]\nagent_secret = "{TENANT_SECRET}"\n'
        f'db_password = "{TENANT_DB_PW}"\nvault_layer = "Notes"\n', encoding="utf-8")
    return repo


# What makes `KB_SVC_DRY_RUN=1` safe: run() prints and exits before starting anything, and
# wait_for_db returns at once. A script missing either would START REAL SERVICES under these
# tests (an early red run against the pre-dry-run script did exactly that), so every dry run
# checks the script text first and refuses to execute it otherwise.
_DRY_RUN_HOOKS = ("KB_SVC_DRY_RUN", "  if dry_run; then print_env \"$@\"; exit 0; fi\n",
                  "  dry_run && return 0\n")


def _assert_dry_run_capable(script: str) -> None:
    missing = [h for h in _DRY_RUN_HOOKS if h not in script]
    assert not missing, f"kb-svc.sh lost its dry-run hooks {missing}: refusing to execute it"


def _dry_run(repo: Path, svc: str, *, tenant: str | None = None, cwd: Path | None = None,
             check: bool = True, extra_env: dict[str, str] | None = None
             ) -> subprocess.CompletedProcess:
    _assert_dry_run_capable((ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8"))
    # Every real service directory the `case` can cd into is a tmp stub, never a checkout.
    stubs = {}
    for var in ("KB_MCP_DIR", "KB_FRONTEND", "KB_INFERENCE"):
        stubs[var] = repo.parent / f"stub-{var.lower()}"
        stubs[var].mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(repo.parent), "KB_REPO": str(repo),
           "KB_SVC_DRY_RUN": "1", **{k: str(v) for k, v in stubs.items()}}
    if tenant is not None:
        env["KB_TENANT"] = tenant
    env.update(extra_env or {})
    # /bin/bash, as the launchd plists run it (bash 3.2 on macOS), not whatever is on PATH.
    return subprocess.run(["/bin/bash", str(ROOT / "scripts" / "kb-svc.sh"), svc], env=env,
                          cwd=cwd or repo.parent, capture_output=True, text=True,
                          check=check, timeout=60)



def test_the_dry_run_guard_refuses_a_script_without_its_hooks():
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")
    _assert_dry_run_capable(script)
    for hook in _DRY_RUN_HOOKS:
        with pytest.raises(AssertionError, match="refusing to execute"):
            _assert_dry_run_capable(script.replace(hook, ""))

def _env_lines(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


@pytest.mark.parametrize("svc", ["ingest", "reanalyze", "resolve", "consolidate"])
def test_kb_svc_dry_run_tenant_mode_resolves_the_tenants_environment(tmp_path, svc):
    """Run the real tenant block for a fixture registry and read back what a
    tenant daemon would be started with — no grep of the script text."""
    from kb_core.tenants import tenant_neutral_policy_path
    repo = _fixture_repo(tmp_path)
    res = _dry_run(repo, svc, tenant="alice")
    env = _env_lines(res.stdout)
    assert env["KB_TENANT"] == "alice"
    # The tenant's own login, never the owner's app role.
    assert env["KB_DATABASE_URL"] == "postgresql://kb_t_alice:***@localhost:5433/kb_t_alice"
    assert env["KB_POLICY_CONFIG"] == str(tenant_neutral_policy_path())   # never ./policy.toml
    assert env["KB_TENANTS_CONFIG"] == f"{repo}/tenants.toml"            # absolute
    assert env["KB_VAULT_LAYER"] == "Notes"
    assert env["KB_INFERENCE_ROUTING"] == f"{repo}/inference.tenant.toml"
    assert env["KB_SOURCES_CONFIG"] == f"{repo}/sources.tenant.toml"
    assert env["KB_INGEST_RECEIVER"] == "0"
    assert env["KB_POOL_MIN_SIZE"] == "1"                                # idle floor
    assert env["KB_ENRICH_CONCURRENCY"] == "2"                           # not the owner's 8
    assert "KB_OBSIDIAN_VAULT" not in env or env["KB_OBSIDIAN_VAULT"] == ""
    assert "kb_core" not in env["EXEC"] or svc in env["EXEC"]
    assert SECRET_PW not in res.stdout + res.stderr                     # never printed
    assert TENANT_DB_PW not in res.stdout + res.stderr
    assert "owner-hmac" not in res.stdout


def test_kb_svc_dry_run_unknown_tenant_aborts_before_anything_runs(tmp_path):
    repo = _fixture_repo(tmp_path)
    res = _dry_run(repo, "ingest", tenant="nobody", check=False)
    assert res.returncode != 0
    assert "EXEC=" not in res.stdout and "KB_DATABASE_URL" not in res.stdout


def test_kb_svc_dry_run_owner_mode_keeps_the_owners_values(tmp_path):
    repo = _fixture_repo(tmp_path)
    env = _env_lines(_dry_run(repo, "ingest").stdout)
    assert env["KB_TENANT"] == "owner"
    assert env["KB_POLICY_CONFIG"] == f"{repo}/policy.toml"
    assert env["KB_DATABASE_URL"] == "postgresql://kb_app:***@localhost:5433/kb_mcp"
    assert env["KB_TENANTS_CONFIG"] == f"{repo}/tenants.toml"
    assert "KB_POOL_MIN_SIZE" not in env


def test_kb_svc_dry_run_mcp_reads_the_kb_core_registry_from_its_own_cwd(tmp_path):
    """mcp runs after `cd $KB_MCP_DIR`; the registry path it is given is absolute."""
    repo = _fixture_repo(tmp_path)
    env = _env_lines(_dry_run(repo, "mcp").stdout)
    assert env["KB_TENANTS_CONFIG"] == f"{repo}/tenants.toml"
    assert env["EXEC"].endswith("kb-mcp")


def test_kb_svc_dry_run_relative_registry_path_resolves_against_kb_core(tmp_path):
    repo = _fixture_repo(tmp_path)
    with (repo / ".env").open("a", encoding="utf-8") as f:
        f.write("KB_TENANTS_CONFIG=tenants.toml\n")
    env = _env_lines(_dry_run(repo, "mcp").stdout)
    assert env["KB_TENANTS_CONFIG"] == f"{repo}/tenants.toml"


def test_kb_svc_tenant_block_reads_the_lookup_as_an_assignment():
    """Structural half that behaviour cannot show: the lookup is an assignment (set -e sees
    its exit status) and the owner's values are cleared before the tenant's are read."""
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")
    block = script.split("# --- Tenant mode", 1)[1].split("# --- End tenant mode", 1)[0]
    assert 'tenant_env="$(' in block
    assert "< <(" not in block
    assert block.index("unset KB_DATABASE_URL") < block.index("while IFS= read -r line")


def test_kb_svc_ingest_only_demands_the_hmac_secret_when_the_receiver_runs():
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")
    block = script.split("ingest)", 1)[1].split(";;", 1)[0]
    assert '"${KB_INGEST_RECEIVER:-1}"' in block
    assert 'if [[ ! "$receiver" =~ ^(' in block
    assert 'if [[ -z "${KB_INGEST_SECRET:-}" ]]' in block       # unchanged contract


def _receiver_switch() -> str:
    """The receiver switch, verbatim from kb-svc.sh's ingest block."""
    block = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8").split("ingest)", 1)[1]
    start = block.index('    receiver="$(')
    end = block.index("\n    fi\n", start) + len("\n    fi\n")
    return block[start:end]


def test_kb_svc_receiver_switch_accepts_exactly_the_daemons_off_values():
    """kb-svc.sh and receiver_enabled() must agree on what 'off' means, or a value one
    side reads as off (say 'false') demands the HMAC secret on the other."""
    from kb_core.ingest.daemon import RECEIVER_OFF_VALUES
    off = re.search(r'=~ \^\(([0-9a-z|]+)\)\$', _receiver_switch())
    assert off is not None
    assert set(off.group(1).split("|")) == set(RECEIVER_OFF_VALUES)


@pytest.mark.parametrize("value, needs_secret", [
    ("0", False), ("false", False), (" OFF ", False), ("No", False),
    ("1", True), ("", True), ("yes", True), ("o ff", True),
])
def test_kb_svc_receiver_switch_behaves_like_the_daemon(value, needs_secret):
    """Run the switch itself under bash; it must agree with receiver_enabled()."""
    from kb_core.ingest.daemon import receiver_enabled
    snippet = _receiver_switch().replace("export KB_INGEST_SECRET", "echo NEEDS_SECRET")
    out = subprocess.run(["bash", "-c", "log() { :; }\n" + snippet],
                         env={"PATH": os.environ["PATH"], "KB_INGEST_RECEIVER": value,
                              "KB_INGEST_SECRET": "x"},
                         capture_output=True, text=True, check=True).stdout
    assert ("NEEDS_SECRET" in out) is needs_secret
    assert receiver_enabled({"KB_INGEST_RECEIVER": value}) is needs_secret


def test_kb_svc_has_gateway_and_backup_blocks():
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")
    gw = script.split("agent-gateway)", 1)[1].split(";;", 1)[0]
    assert "wait_for_db" in gw and "kb-agent-gateway" in gw and 'KB_GATEWAY_PORT="${KB_GATEWAY_PORT:-8040}"' in gw
    bk = script.split("backup)", 1)[1].split(";;", 1)[0]
    assert "kb-backup run" in bk and 'KB_DATABASE_URL="$DBURL"' in bk


def test_kb_dev_writes_tenant_plists_and_supervises_new_services():
    script = (ROOT / "scripts" / "kb-dev.sh").read_text(encoding="utf-8")
    services = next(line for line in script.splitlines() if line.startswith("SERVICES=("))
    assert "agent-gateway" in services and "backup" in services
    assert "TENANT_SERVICES=(ingest reanalyze resolve consolidate)" in script
    # EnvironmentVariables is a plist <dict>: the variable name is a <key>, its value a
    # <string> (`<string>KB_TENANT</string>` alone would be an invalid plist).
    assert "write_tenant_plist" in script
    tenant_plist = script.split("write_tenant_plist() {", 1)[1].split("\n}\n", 1)[0]
    assert "<key>KB_TENANT</key>\n        <string>$slug</string>" in tenant_plist
    assert 'tlabel() { echo "${KB_SERVICE_PREFIX:-com.kb}.t.$1.$2"; }' in script
    # stop/uninstall/status/restart must reach tenants disabled after install.
    assert "tenant_slugs all" in script
    slugs_fn = script.split("tenant_slugs() {", 1)[1].split("\n}\n", 1)[0]
    assert "include_disabled" in slugs_fn
    # Gate on the same registry path load_registry() reads.
    assert "${KB_TENANTS_CONFIG:-tenants.toml}" in slugs_fn
    # ...which kb-dev exports absolute, like kb-svc.sh.
    assert 'KB_TENANTS_CONFIG="${KB_TENANTS_CONFIG:-$KB_REPO/tenants.toml}"' in script
    assert script.index("export KB_TENANTS_CONFIG") < script.index("tenant_slugs() {")


def test_kb_svc_binds_inference_to_loopback_by_default():
    """KB_Inference holds the model and session history; it must not follow a stray HOST
    onto the network. A non-loopback bind is opt-in through KB_INFERENCE_HOST (and
    server.py then also demands KB_INFERENCE_ALLOW_REMOTE + KB_INFERENCE_TOKEN)."""
    script = (ROOT / "scripts" / "kb-svc.sh").read_text(encoding="utf-8")
    block = script.split("\n  inference)", 1)[1].split(";;", 1)[0]
    assert 'HOST="${KB_INFERENCE_HOST:-127.0.0.1}"' in block


def test_kb_dev_uses_the_current_docker_context_unless_kb_docker_context_is_set():
    """No hard-coded context: a stock install (one docker daemon, any context name) must
    work as-is. KB_DOCKER_CONTEXT pins one, and is applied AFTER KB_Core/.env is sourced
    so a value set there takes effect."""
    script = (ROOT / "scripts" / "kb-dev.sh").read_text(encoding="utf-8")
    assert "desktop-linux" not in script
    pin = 'if [[ -n "${KB_DOCKER_CONTEXT:-}" ]]; then export DOCKER_CONTEXT="$KB_DOCKER_CONTEXT"; fi'
    assert pin in script
    assert script.index("source .env") < script.index(pin)
    assert "export DOCKER_CONTEXT=" not in script.replace(pin, "")

    snippet = script[script.index(pin):].split("\n", 1)[0]
    for env, want in (({}, ""), ({"KB_DOCKER_CONTEXT": "ctx-a"}, "ctx-a")):
        out = subprocess.run(["bash", "-c", snippet + '\nprintf %s "${DOCKER_CONTEXT:-}"'],
                             env={"PATH": os.environ["PATH"], **env},
                             capture_output=True, text=True, check=True).stdout
        assert out == want


def test_kb_dev_guards_mac_only_tools():
    """brew and `open -a Docker` exist only on a Mac (brew: only with Homebrew), so each
    call sits behind a check instead of failing a set -e script outright."""
    script = (ROOT / "scripts" / "kb-dev.sh").read_text(encoding="utf-8")
    docker = script.split("ensure_docker() {", 1)[1].split("\n}\n", 1)[0]
    assert "open -a Docker" in docker
    assert docker.index('"$(uname -s)" == Darwin') < docker.index("open -a Docker")
    assert "command -v open" in docker

    ollama = script.split("ensure_ollama() {", 1)[1].split("\n}\n", 1)[0]
    assert ollama.index("command -v brew") < ollama.index("brew services")


# --- the inference venv must be KB_Inference's own, whatever VIRTUAL_ENV says -----------

def _fake_inference_python(stub: Path, *, imports_ok: bool) -> Path:
    """A KB_Inference/.venv/bin/python stand-in that records the VIRTUAL_ENV it saw and
    answers the dependency probe as told."""
    py = stub / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    seen = stub / "virtual_env_seen"
    py.write_text(f'#!/bin/sh\nprintf "%s" "${{VIRTUAL_ENV-unset}}" > "{seen}"\n'
                  f'exit {0 if imports_ok else 1}\n', encoding="utf-8")
    py.chmod(0o755)
    return seen


def test_kb_svc_inference_installs_into_its_own_venv_under_an_inherited_virtual_env(tmp_path):
    """`uv run --project KB_Core kb-run` exports VIRTUAL_ENV=KB_Core/.venv; a bare
    `uv pip install` then installed KB_Inference's deps THERE and inference crash-looped."""
    repo = _fixture_repo(tmp_path)
    res = _dry_run(repo, "inference",
                   extra_env={"VIRTUAL_ENV": str(repo / ".venv")})
    assert "skipping uv venv" in res.stderr
    assert "skipping uv pip install --python .venv/bin/python -r requirements.txt" in res.stderr


def test_kb_svc_inference_reinstalls_when_the_venv_exists_but_its_deps_do_not(tmp_path):
    """The old guard was `-x .venv/bin/python`: a venv whose install went elsewhere was
    never repaired. Now a failed import re-runs the install (without recreating the venv),
    and the probe runs with VIRTUAL_ENV unset."""
    repo = _fixture_repo(tmp_path)
    seen = _fake_inference_python(repo.parent / "stub-kb_inference", imports_ok=False)
    res = _dry_run(repo, "inference", extra_env={"VIRTUAL_ENV": str(repo / ".venv")})
    assert "skipping uv venv" not in res.stderr
    assert "skipping uv pip install --python .venv/bin/python" in res.stderr
    assert seen.read_text(encoding="utf-8") == "unset"


def test_kb_svc_inference_skips_the_install_when_its_deps_import(tmp_path):
    repo = _fixture_repo(tmp_path)
    _fake_inference_python(repo.parent / "stub-kb_inference", imports_ok=True)
    res = _dry_run(repo, "inference")
    assert "skipping uv" not in res.stderr
    assert _env_lines(res.stdout)["EXEC"] == ".venv/bin/python server.py"


@pytest.mark.parametrize("svc", ["backend", "ingest", "reanalyze", "resolve", "consolidate",
                                 "mcp", "healthcheck"])
def test_kb_svc_every_inference_client_follows_kb_inference_port(tmp_path, svc):
    """The backend used to get no KB_INFERENCE_URL and fell back to :8001 — after the setup
    guide's 'port taken, move KB_INFERENCE_PORT' advice, that is another install's server."""
    repo = _fixture_repo(tmp_path)
    with (repo / ".env").open("a", encoding="utf-8") as f:
        f.write("KB_INFERENCE_PORT=58801\n")
    env = _env_lines(_dry_run(repo, svc).stdout)
    assert env["KB_INFERENCE_URL"] == "http://127.0.0.1:58801"


# --- a second install gets its own compose project, container and volume ---------------

def test_compose_project_and_container_follow_kb_compose_project():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "\nname: ${KB_COMPOSE_PROJECT:-kb_mcp}\n" in compose
    assert "container_name: ${KB_COMPOSE_PROJECT:-kb_mcp}_db\n" in compose
    kb_dev = (ROOT / "scripts" / "kb-dev.sh").read_text(encoding="utf-8")
    assert 'KB_DB_CONTAINER="${KB_COMPOSE_PROJECT:-kb_mcp}_db"' in kb_dev
    assert "' kb_mcp_db" not in kb_dev             # no docker inspect of a fixed name


@pytest.mark.skipif(not shutil.which("docker"), reason="docker CLI not installed")
@pytest.mark.parametrize("project, container, volume", [
    (None, "kb_mcp_db", "kb_mcp_kb_pgdata"),      # the default: existing installs unchanged
    ("kb_second", "kb_second_db", "kb_second_kb_pgdata"),
])
def test_compose_renders_isolated_names(tmp_path, project, container, volume):
    """`docker compose config` only renders the file (no daemon is contacted)."""
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(tmp_path)),
           "POSTGRES_PASSWORD": "x", "KB_APP_PASSWORD": "y"}
    if project:
        env["KB_COMPOSE_PROJECT"] = project
    res = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"),
                          "--project-directory", str(tmp_path), "config"],
                         env=env, capture_output=True, text=True, timeout=60)
    if res.returncode != 0 and "is not a docker command" in res.stderr:
        pytest.skip("docker compose plugin not installed")
    assert res.returncode == 0, res.stderr
    assert f"container_name: {container}\n" in res.stdout
    assert f"name: {volume}\n" in res.stdout
