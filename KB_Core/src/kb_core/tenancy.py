"""Request-scoped tenant context: which database this call touches.

A surface (MCP tool wrapper, dashboard middleware, gateway dependency, a daemon's main)
binds a TenantContext with `tenant_scope(ctx)` before any engine code runs; engine code
reaches the bound KnowledgeBase through `require_tenant().kb` or, with zero call-site
edits, through a `TenantKBProxy`. Unbound access raises — never a cross-tenant default.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from kb_core.db import migrate_pool, open_pool
from kb_core.knowledge_base import KnowledgeBase
from kb_core.policy import Policy, Prompts, load_policy
from kb_core.registry import default_registry
from kb_core.tenants import OWNER_SLUG, Registry, RegistryError, Tenant, load_registry


class TenantUnresolved(RuntimeError):
    """A code path touched the database before any tenant was bound to this request."""


@dataclass
class TenantContext:
    tenant: Tenant
    pool: ConnectionPool | None
    kb: KnowledgeBase


current_tenant: ContextVar[TenantContext | None] = ContextVar("kb_current_tenant", default=None)
current_principal: ContextVar[str | None] = ContextVar("kb_current_principal", default=None)


def require_tenant() -> TenantContext:
    ctx = current_tenant.get()
    if ctx is None:
        raise TenantUnresolved("no tenant bound to this request")
    return ctx


def default_tenant_slug(env=None) -> str:
    """The tenant an UNAUTHENTICATED surface serves: the local MCP listener, a dashboard
    with auth off, a daemon. KB_TENANT, default 'owner'."""
    env = os.environ if env is None else env
    return (env.get("KB_TENANT") or OWNER_SLUG).strip()


@contextmanager
def tenant_scope(ctx: TenantContext):
    token = current_tenant.set(ctx)
    try:
        yield ctx
    finally:
        current_tenant.reset(token)


def policy_for(tenant: Tenant, env=None) -> Policy:
    env = os.environ if env is None else env
    if tenant.policy_path is not None:
        # A non-owner tenant's file never inherits the owner's cwd-relative prompts.local.
        return load_policy(env, path=str(tenant.policy_path), tenant=not tenant.is_owner)
    if tenant.prompts_overlay is not None:
        return Policy(prompts=Prompts(overlay_dir=str(tenant.prompts_overlay)))
    if tenant.is_owner:
        return load_policy(env)          # today's behaviour: KB_POLICY_CONFIG / ./policy.toml
    return Policy()


def daemon_policy(env=None) -> Policy | None:
    """The policy a daemon main() builds its KnowledgeBase with. Owner (KB_TENANT unset or
    "owner"): None, so KnowledgeBase keeps today's load_policy(). Tenant: the registry's
    policy for that slug — the same policy_for the shared processes use, so a tenant's
    daemons and its MCP/dashboard agree, and the owner's ./policy.toml + prompts.local/
    are never read. An unknown slug is a RegistryError (fail loud, never the owner's)."""
    env = os.environ if env is None else env
    slug = default_tenant_slug(env)
    if slug == OWNER_SLUG:
        return None
    tenant = load_registry(env=env).by_slug(slug)
    if tenant is None:
        raise RegistryError(f"KB_TENANT={slug!r} is not in the tenant registry")
    return policy_for(tenant, env)


class TenantPools:
    """One pool + one KnowledgeBase per tenant, opened lazily and migrated on first open
    (idempotent: `kb-tenant create` runs every migration before a service can see the
    tenant). Tenant pools default to KB_TENANT_POOL_MAX (4); the owner may be larger."""

    def __init__(self, registry: Registry, *, max_size: int | None = None,
                 owner_max_size: int | None = None, env=None, migrate: bool = True) -> None:
        self.registry = registry
        # Forwarded as given: None lets dsn_for use the REGISTRY's env (a registry built
        # with its own env must not have it overridden by os.environ) and policy_for its own.
        self._env = env
        pool_env = os.environ if env is None else env
        self._max = max_size if max_size is not None else int(pool_env.get("KB_TENANT_POOL_MAX", "4"))
        self._owner_max = owner_max_size
        self._migrate = migrate
        self._ctx: dict[str, TenantContext] = {}
        self._lock = threading.Lock()

    def get(self, tenant: Tenant) -> TenantContext:
        ctx = self._ctx.get(tenant.slug)
        if ctx is not None:
            return ctx
        with self._lock:
            ctx = self._ctx.get(tenant.slug)
            if ctx is None:
                dsn = self.registry.dsn_for(tenant, self._env)
                if tenant.is_owner:
                    # The owner keeps today's pool: open_pool's defaults, or its own ceiling.
                    pool = (open_pool(dsn, max_size=self._owner_max) if self._owner_max
                            else open_pool(dsn))
                else:
                    # min_size=1: every shared process opens every tenant it serves, so an
                    # eager floor of 4 would pin 4 idle connections per tenant per process.
                    pool = open_pool(dsn, max_size=self._max, min_size=1)
                if self._migrate:
                    try:
                        migrate_pool(pool)
                    except BaseException:
                        with suppress(Exception):  # the migration error stays primary
                            pool.close()           # a failed first open must not leak a pool
                        raise
                kb = KnowledgeBase(pool, default_registry(), policy=policy_for(tenant, self._env))
                ctx = TenantContext(tenant=tenant, pool=pool, kb=kb)
                self._ctx[tenant.slug] = ctx
        return ctx

    def get_by_slug(self, slug: str) -> TenantContext:
        t = self.registry.by_slug(slug)
        if t is None:
            raise LookupError(f"unknown tenant {slug!r}")
        return self.get(t)

    def opened(self) -> list[str]:
        return sorted(self._ctx)

    def close(self) -> None:
        with self._lock:
            for ctx in self._ctx.values():
                if ctx.pool is not None:
                    ctx.pool.close()
            self._ctx.clear()


class TenantKBProxy:
    """A KnowledgeBase-shaped object that forwards every attribute to the CURRENT tenant's
    KnowledgeBase. Code written as `kb.search(...)` serves many tenants with no per-call-
    site edit; the surface binds the tenant before the body runs. Unbound use raises."""

    def __getattr__(self, name: str):
        return getattr(require_tenant().kb, name)
