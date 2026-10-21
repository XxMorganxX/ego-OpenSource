# KB_Core/tests/test_tenant_profiles.py
import tomllib
from pathlib import Path

from kb_core.inference_routing import TASKS

ROOT = Path(__file__).resolve().parent.parent


def test_inference_tenant_profile_routes_everything_to_the_agent_except_embed():
    data = tomllib.loads((ROOT / "inference.tenant.toml").read_text())
    tasks = data["task"]
    assert set(tasks) == set(TASKS)
    for name, block in tasks.items():
        assert block["provider"] == ("local" if name == "embed" else "agent"), name
    assert tasks["curate_parse"]["timeout"] == 60
    assert data["provider"]["agent"] == {"concurrency": 8, "timeout": 600}


def test_agent_job_timeout_env_overrides_the_profiles_provider_timeout():
    """KB_AGENT_JOB_TIMEOUT overrides [provider.agent] timeout. It was dead
    for producers (the profile always sets 600). An explicit per-task timeout (curate_parse's
    interactive 60 s) is the task's own choice and is kept."""
    from kb_core.inference_routing import RoutingError, load_routing
    path = str(ROOT / "inference.tenant.toml")
    base = load_routing({}, path)
    assert base["extract_entities"].timeout == 600 and base["curate_parse"].timeout == 60
    routes = load_routing({"KB_AGENT_JOB_TIMEOUT": "120"}, path)
    assert routes["extract_entities"].timeout == 120.0
    assert routes["curate_parse"].timeout == 60
    # InferenceRouter._client forwards route.timeout as build_inference_client's `timeout`.
    import pytest
    for bad in ("0", "-5", "soon"):
        with pytest.raises(RoutingError, match="KB_AGENT_JOB_TIMEOUT"):
            load_routing({"KB_AGENT_JOB_TIMEOUT": bad}, path)


def test_sources_tenant_profile_enables_only_vault():
    data = tomllib.loads((ROOT / "sources.tenant.toml").read_text())
    assert data["vault"] == {"enabled": True, "is_enrichable": True, "period_seconds": 20}
    assert data["webhook"] == {"enabled": False}
    assert "obsidian" not in data


def test_pyproject_declares_the_new_entry_points_and_jwt():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'kb-tenant = "kb_core.tenant_cli:main"' in text
    assert 'kb-agent-gateway = "kb_core.gateway.app:main"' in text
    assert 'kb-backup = "kb_core.backup:main"' in text
    assert '"pyjwt[crypto]>=2.8"' in text


def test_compose_raises_max_connections_for_per_tenant_pools():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "max_connections=300" in text
