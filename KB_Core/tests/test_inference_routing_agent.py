from pathlib import Path

import pytest

from kb_core.inference_routing import TASKS, InferenceRouter, RoutingError, load_provider_limits, load_routing
from kb_core.ingest.agent_queue_client import AgentQueueClient
from kb_core.ingest.inference import InferenceError, build_inference_client

ROOT = Path(__file__).resolve().parent.parent


def _tenant_toml(tmp_path, extra: str = "") -> str:
    body = '[provider.agent]\nconcurrency = 8\ntimeout = 600\n' + extra
    body += "".join(f'[task.{n}]\nprovider = "{"local" if n == "embed" else "agent"}"\n' for n in TASKS)
    p = tmp_path / "inference.toml"
    p.write_text(body)
    return str(p)


def test_shipped_tenant_profile_validates():
    routes = load_routing(path=str(ROOT / "inference.tenant.toml"))
    assert routes["embed"].provider == "local"
    assert all(r.provider == "agent" for n, r in routes.items() if n != "embed")
    assert routes["curate_parse"].timeout == 60
    assert routes["doc_kind"].timeout == 600                       # provider-block default
    assert load_provider_limits(path=str(ROOT / "inference.tenant.toml"))["agent"] == 8


def test_agent_provider_block_accepts_timeout_but_local_does_not(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text('[provider.local]\ntimeout = 5\n'
                 + "".join(f'[task.{n}]\nprovider = "local"\n' for n in TASKS))
    with pytest.raises(RoutingError, match="timeout"):
        load_routing(path=str(p))


def test_embed_may_not_route_to_agent(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("".join(f'[task.{n}]\nprovider = "agent"\n' for n in TASKS))
    with pytest.raises(RoutingError, match=r"task\.embed"):
        load_routing(path=str(p))


def test_factory_builds_agent_client_only_with_a_pool(pool):
    with pytest.raises(InferenceError, match="pool"):
        build_inference_client({}, provider="agent", task="doc_kind")
    c = build_inference_client({"KB_AGENT_JOB_TIMEOUT": "7"}, provider="agent", task="doc_kind", pool=pool)
    assert isinstance(c, AgentQueueClient) and c.provider == "agent" and c._timeout == 7.0


def test_router_hands_the_pool_and_task_to_agent_clients(tmp_path, pool):
    router = InferenceRouter({}, _tenant_toml(tmp_path), pool=pool)
    a = router.client_for("doc_kind")
    b = router.client_for("extract_entities")
    assert a is not b                                       # one client per task for `agent`
    inner_a = getattr(a, "_inner", a)                       # ProviderLimiter wraps it
    assert isinstance(inner_a, AgentQueueClient) and inner_a._task == "doc_kind"
    assert router.embedder() is not None                    # local, bare HTTP client


def test_router_without_pool_fails_loud_for_agent_routes(tmp_path):
    router = InferenceRouter({}, _tenant_toml(tmp_path))
    with pytest.raises(InferenceError, match="pool"):
        router.client_for("doc_kind")
