import os

import pytest

from kb_core.db import disposable_db_refusal, migrate_pool, open_pool
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry
from tests.legacy_policy import LEGACY_POLICY

TEST_DATABASE_URL = os.environ.get(
    "KB_TEST_DATABASE_URL", "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test"
)


def tenant_login(url: str | None = None) -> dict:
    """`db_role`/`db_password` for a test Tenant: the test cluster's own login (from `url`,
    default TEST_DATABASE_URL). Production tenants each get their own role from
    `kb-tenant create`, and Registry.dsn_for refuses a tenant without one, so every test
    Tenant that reaches dsn_for says which login it uses."""
    from urllib.parse import unquote, urlsplit
    parts = urlsplit(url or TEST_DATABASE_URL)
    return {"db_role": unquote(parts.username or ""), "db_password": unquote(parts.password or "")}


def pytest_configure(config):
    """The fixtures below TRUNCATE their database: refuse a DSN that isn't a test database
    (or is the live KB_DATABASE_URL) before anything connects."""
    for url in (TEST_DATABASE_URL, *filter(None, [os.environ.get("KB_TEST_DATABASE_URL_B")])):
        refusal = disposable_db_refusal(url)
        if refusal:
            raise pytest.UsageError(refusal)


@pytest.fixture(scope="session")
def pool():
    pool = open_pool(TEST_DATABASE_URL)
    migrate_pool(pool)
    yield pool
    pool.close()


@pytest.fixture
def kb(pool):
    with pool.connection() as conn:
        conn.execute("TRUNCATE nodes CASCADE")
        conn.execute("TRUNCATE ingest_log")
        conn.execute("TRUNCATE ingest_inbox")
        conn.execute("TRUNCATE mcp_trace CASCADE")   # CASCADE clears retrieval_judgment
        # mcp_tool_state has no FK to nodes, so it survives TRUNCATE nodes CASCADE.
        conn.execute("TRUNCATE mcp_tool_state")
        # entity_instance_blocks has no FK to nodes, so it survives TRUNCATE nodes CASCADE —
        # clear it explicitly for per-test isolation.
        conn.execute("TRUNCATE entity_instance_blocks")
        # concept_runs/concepts have no FK to nodes, so they survive TRUNCATE nodes
        # CASCADE — clear the whole derived concept layer explicitly (like blocks above).
        # concept_run_requests references concept_runs, so it must be truncated in the
        # SAME statement or Postgres refuses the parent.
        conn.execute("TRUNCATE concept_runs, concept_run_requests, concepts, "
                     "concept_members, concept_constraints, "
                     "concept_member_overrides, concept_separations")
        # provider_pause has no FK to nodes, so it survives TRUNCATE nodes CASCADE —
        # clear it so one test's outage can't gate another test's enrichment lane.
        conn.execute("TRUNCATE provider_pause")
        # The tenancy tables have no FK to nodes either: sync_state holds per-source
        # cursors (a vault manifest generation would otherwise leak between tests and make
        # later manifests stale), llm_jobs the agent queue, agent_state one heartbeat row.
        conn.execute("TRUNCATE sync_state, llm_jobs")
        conn.execute("UPDATE agent_state SET last_seen_at = NULL, agent_version = NULL, "
                     "hostname = NULL")
        # age_decay_state has no FK to nodes — reset the master switch so one test's
        # flip can't leak decay-off behavior into another.
        conn.execute("UPDATE age_decay_state SET enabled = true")
        # The skill registry has no FK to nodes: clear it so one test's skills (and
        # their immutable revisions) never leak into another test's library.
        conn.execute("TRUNCATE skills CASCADE")
    return KnowledgeBase(pool, default_registry(), policy=LEGACY_POLICY)


@pytest.fixture(autouse=True)
def _reset_inference_limits():
    """No test may inherit another test's provider-limit registry state (see
    kb_core.inference_limits): reset before AND after every test in the process."""
    from kb_core.inference_limits import reset_limits
    reset_limits()
    yield
    reset_limits()


@pytest.fixture
def routing_toml(tmp_path):
    """A complete inference.toml (all registered tasks) for router-based wiring tests:
    claude-cli everywhere except the tasks pinned local in the repo file. Returns the
    path string, ready for KB_INFERENCE_ROUTING or InferenceRouter(path=...)."""
    from kb_core.inference_routing import TASKS
    local = {"doc_kind", "describe_entity", "merge_judge", "embed"}
    p = tmp_path / "inference.toml"
    p.write_text(
        '[provider.claude-cli]\nmodel = "claude-sonnet-5"\n'
        + "".join(f'[task.{n}]\nprovider = "{"local" if n in local else "claude-cli"}"\n'
                  for n in TASKS))
    return str(p)
