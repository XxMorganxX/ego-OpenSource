import os
import threading

import pytest

from kb_core import tenancy as Y
from kb_core.domain import NodeRecord
from kb_core.tenants import Registry, Tenant
from tests.conftest import TEST_DATABASE_URL, tenant_login

URL_B = os.environ.get("KB_TEST_DATABASE_URL_B",
                       TEST_DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_DATABASE_URL.rsplit("/", 1)[1] + "2")


def _registry() -> Registry:
    owner = Tenant("owner", frozenset({"m@example.com"}), database=TEST_DATABASE_URL.rsplit("/", 1)[1])
    a = Tenant("a", frozenset({"a@example.com"}), database=TEST_DATABASE_URL.rsplit("/", 1)[1], agent_secret="a" * 64, **tenant_login())
    b = Tenant("b", frozenset({"b@example.com"}), database=URL_B.rsplit("/", 1)[1], agent_secret="b" * 64, **tenant_login())
    return Registry(owner, {"a": a, "b": b}, env={"KB_DATABASE_URL": TEST_DATABASE_URL})


def test_require_tenant_raises_when_unbound():
    with pytest.raises(Y.TenantUnresolved):
        Y.require_tenant()
    with pytest.raises(Y.TenantUnresolved):
        Y.TenantKBProxy().healthcheck()


def test_default_tenant_slug_reads_env(monkeypatch):
    monkeypatch.delenv("KB_TENANT", raising=False)
    assert Y.default_tenant_slug() == "owner"
    monkeypatch.setenv("KB_TENANT", "alice")
    assert Y.default_tenant_slug() == "alice"


def test_pools_open_lazily_and_proxy_forwards(kb):
    pools = Y.TenantPools(_registry(), max_size=2)
    try:
        assert pools.opened() == []
        ctx = pools.get_by_slug("a")
        assert pools.opened() == ["a"] and ctx.tenant.slug == "a"
        assert pools.get_by_slug("a") is ctx                  # cached
        proxy = Y.TenantKBProxy()
        with Y.tenant_scope(ctx):
            n = proxy.upsert(NodeRecord(source="agent", external_id="t1", type="note",
                                        content="hi", slug="t1"))
            assert proxy.get(n.id).content == "hi"
        with pytest.raises(Y.TenantUnresolved):
            proxy.get(n.id)                                    # scope ended
    finally:
        pools.close()


def test_tenant_pools_start_with_one_connection_owner_keeps_defaults(kb):
    pools = Y.TenantPools(_registry(), max_size=2)
    try:
        assert pools.get_by_slug("a").pool.min_size == 1      # no idle pinning per tenant
        assert pools.get_by_slug("owner").pool.min_size == 4  # owner: open_pool defaults
    finally:
        pools.close()


def test_scope_is_per_thread_context(kb):
    pools = Y.TenantPools(_registry(), max_size=2)
    seen: dict[str, str] = {}

    def worker(slug: str) -> None:
        with Y.tenant_scope(pools.get_by_slug(slug)):
            seen[slug] = Y.require_tenant().tenant.slug
    try:
        t = threading.Thread(target=worker, args=("a",))
        with Y.tenant_scope(pools.get_by_slug("owner")):
            t.start(); t.join()
            assert Y.require_tenant().tenant.slug == "owner"
        assert seen == {"a": "a"}
    finally:
        pools.close()


def test_second_tenant_database_is_separate(kb):
    import psycopg
    try:
        psycopg.connect(URL_B, connect_timeout=3).close()
    except Exception:
        pytest.skip("KB_TEST_DATABASE_URL_B unreachable")
    pools = Y.TenantPools(_registry(), max_size=2)
    try:
        a, b = pools.get_by_slug("a"), pools.get_by_slug("b")
        with b.pool.connection() as c:
            c.execute("TRUNCATE nodes CASCADE")
        a.kb.upsert(NodeRecord(source="agent", external_id="only-a", type="note", content="a", slug="only_a"))
        assert a.kb.get_by_path("only_a") is not None
        assert b.kb.get_by_path("only_a") is None
    finally:
        pools.close()


def test_policy_for_owner_uses_load_policy_and_tenant_defaults(tmp_path):
    owner = Tenant("owner", frozenset(), database="x")
    plain = Tenant("t", frozenset(), database="kb_t_t", agent_secret="s" * 64, **tenant_login())
    from kb_core.policy import Policy
    assert isinstance(Y.policy_for(owner, env={}), Policy)        # owner: today's load_policy path
    assert Y.policy_for(plain, env={}).prompts.overlay_dir is None
    overlay = Tenant("u", frozenset(), database="kb_t_u", agent_secret="s" * 64, **tenant_login(),
                     prompts_overlay=tmp_path / "ov")
    assert Y.policy_for(overlay, env={}).prompts.overlay_dir == str(tmp_path / "ov")


def test_failed_migration_closes_the_pool_and_caches_nothing(monkeypatch):
    opened = []
    real_open = Y.open_pool

    def spy_open(*a, **kw):
        opened.append(real_open(*a, **kw))
        return opened[-1]

    def boom(pool):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(Y, "open_pool", spy_open)
    monkeypatch.setattr(Y, "migrate_pool", boom)
    pools = Y.TenantPools(_registry(), max_size=2)
    with pytest.raises(RuntimeError, match="migration failed"):
        pools.get_by_slug("a")
    assert len(opened) == 1 and opened[0].closed
    assert pools.opened() == []
