import os

import pytest

from kb_core.db import disposable_db_refusal, migrate_pool, open_pool
from kb_core.knowledge_base import KnowledgeBase
from kb_core.policy import Policy
from kb_core.registry import default_registry

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
        conn.execute("TRUNCATE mcp_trace CASCADE")
        # mcp_tool_state has no FK to nodes, so it survives TRUNCATE nodes CASCADE.
        conn.execute("TRUNCATE mcp_tool_state")
        # concepts survive TRUNCATE nodes CASCADE (no FK to nodes) — clear the derived
        # layer explicitly, mirroring KB_Core's conftest.
        # concept_run_requests references concept_runs, so it must be truncated in the
        # SAME statement or Postgres refuses the parent (mirrors KB_Core/tests/conftest.py).
        conn.execute("TRUNCATE concept_runs, concept_run_requests, concepts, "
                     "concept_members, concept_constraints, "
                     "concept_member_overrides, concept_separations")
        # The skill registry has no FK to nodes: clear it so one test's skills (and
        # their immutable revisions) never leak into another test's library.
        conn.execute("TRUNCATE skills CASCADE")
    return KnowledgeBase(pool, default_registry(), policy=Policy())


# The `_no_live_inference_client` guard (autouse fixture that monkeypatched
# kb_retrieval.crawl.judge.build_inference_client to raise) was removed when
# the LLM tier was deleted: it existed because DEFAULT_MCP_HOP_JUDGE (kb_mcp/server.py) historically
# defaulted `judge` to "llm", and its subject, LLMHopJudge and the client it lazily
# built, is deleted along with it -- HOP_JUDGES has exactly one entry ("none") now,
# so there is no path left in this process that builds a real inference client from
# a forgotten `judge=`. KB_Retrieval/tests/conftest.py's identical fixture was
# removed in the same deletion for the same reason.
