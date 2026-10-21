import os

import pytest

from kb_core.db import disposable_db_refusal, migrate_pool, open_pool
from kb_core.knowledge_base import KnowledgeBase
from kb_core.policy import Policy
from kb_core.registry import default_registry

# A DIFFERENT default database than KB_Core's own suite (kb_mcp_test): both suites'
# `kb` fixtures unconditionally `TRUNCATE nodes CASCADE` per test, and they share one
# Postgres instance. With one shared default database, running
# `cd KB_Core && uv run pytest` concurrently with this suite (e.g. one in the
# background while iterating on the other -- an easy thing to do locally) lets one
# process's TRUNCATE fire mid-sequence in the other's, so a just-upserted row is
# gone before the very next statement looks it up -- LookupError / a RETURNING
# clause matching no row. CI is unaffected: its KB_TEST_DATABASE_URL env var
# override points every suite at the same CI-service database on purpose, and CI
# runs KB_Core then KB_Retrieval sequentially, never concurrently (.github/workflows/ci.yml).
TEST_DATABASE_URL = os.environ.get(
    "KB_TEST_DATABASE_URL",
    "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test_retrieval",
)


def pytest_configure(config):
    """The fixtures below TRUNCATE their database: refuse a DSN that isn't a test database
    (or is the live KB_DATABASE_URL) before anything connects."""
    for url in (TEST_DATABASE_URL,):
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
        conn.execute("TRUNCATE entity_instance_blocks")
        # concept_run_requests references concept_runs, so it must be truncated in the
        # SAME statement or Postgres refuses the parent (mirrors KB_Core/tests/conftest.py).
        conn.execute("TRUNCATE concept_runs, concept_run_requests, concepts, "
                     "concept_members, concept_constraints, "
                     "concept_member_overrides, concept_separations")
    return KnowledgeBase(pool, default_registry(), policy=Policy())


@pytest.fixture
def make_concept(kb):
    """Create one ACTIVE concept through the real write path.

    Concepts have no direct create() primitive -- apply_concept_run is the only way
    in, and it is what production consolidation uses, so seeding through it means
    the tests exercise the same member/member_count bookkeeping the crawl reads.
    `members` is [(node_id, member_kind, score), ...].
    """
    def _make(name, members, *, embedding=None, description=None):
        run = kb.begin_concept_run({})
        kb.apply_concept_run(run, [
            {"key": name, "prev_id": None, "parent_key": None, "level": 0,
             "name": name, "kind": "topic", "description": description,
             "embedding": embedding, "members": members}], [])
        kb.finish_concept_run(run, status="done", stats={})
        return {c["name"]: c for c in kb.active_concepts()}[name]["id"]
    return _make


@pytest.fixture
def make_entity(kb):
    """An active INSTANCE entity, optionally linked to memories via 'mentions'.
    Returns the entity's node id -- the id every crawl primitive keys on."""
    from kb_core.entities import MENTIONS

    def _make(name, mentions=()):
        ent = kb.upsert_entity(name, categories=["tool"])
        for memory_id in mentions:
            kb.link_entity(memory_id, ent.node.id, MENTIONS, weight=0.9)
        return ent.node.id
    return _make
