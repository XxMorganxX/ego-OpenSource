def test_migrate_creates_nodes_table(pool):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT to_regclass('public.nodes') AS t"
        ).fetchone()
    assert row["t"] == "nodes"


def test_ltree_extension_present(pool):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 AS ok FROM pg_extension WHERE extname = 'ltree'"
        ).fetchone()
    assert row is not None


def test_migrate_is_idempotent(pool):
    from kb_core.db import migrate
    with pool.connection() as conn:
        applied = migrate(conn)  # already migrated by the `pool` fixture
    assert applied == []


import os
import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from kb_core.db import open_pool

# Duplicated from tests/conftest.py rather than imported -- the house convention
# for test helpers in this repo (test_crawl_primitives.py's header says the same).
TEST_DATABASE_URL = os.environ.get(
    "KB_TEST_DATABASE_URL", "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test"
)


def test_open_pool_default_max_size_is_unchanged_by_the_new_keyword():
    """psycopg_pool resolves max_size=None to min_size (BasePool._check_size), and min_size defaults to 4. Every existing caller -- the
    ingest daemon, the dashboard API, both conftests -- passes no max_size, so
    ADDING the keyword must leave them on exactly 4. This test exists to pin
    "additive", not to pin the number 4 itself."""
    pool = open_pool(TEST_DATABASE_URL)
    try:
        assert isinstance(pool, ConnectionPool)
        assert pool.min_size == 4
        assert pool.max_size == 4
    finally:
        pool.close()


def test_open_pool_honours_an_explicit_max_size():
    """KB_MCP's main() passes a ceiling here (KB_MCP_POOL_MAX). Once MCP tool
    bodies run in worker threads instead of inline on the event loop, pool
    concurrency is genuinely exercised for the first time and a cluster-crawl
    pins ONE connection in REPEATABLE READ for tens of seconds, so the pool becomes the
    server's real concurrency limit."""
    pool = open_pool(TEST_DATABASE_URL, max_size=8)
    try:
        assert pool.max_size == 8
        assert pool.min_size == 4      # unchanged; only the ceiling moved
    finally:
        pool.close()


def test_turn_grouping_migration_leaves_no_orphan_turns(pool):
    with pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM mcp_trace WHERE turn_id IS NULL").fetchone()["n"] == 0
        # every turn_id points at an opener: a row that is its own turn
        assert conn.execute(
            """SELECT count(*) AS n FROM mcp_trace t
               JOIN mcp_trace o ON o.id = t.turn_id
               WHERE o.turn_id <> o.id""").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT 1 AS ok FROM pg_proc WHERE proname = 'mcp_trace_rebuild_turns'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT to_regclass('public.turn_judgment') AS t").fetchone()["t"] == "turn_judgment"


def test_open_pool_honours_KB_POOL_MIN_SIZE_without_moving_the_ceiling(monkeypatch):
    """Tenant daemons pin KB_POOL_MIN_SIZE=1 (kb-svc.sh) so their idle floor is one
    connection per pool, not four; the ceiling every caller had stays where it was."""
    monkeypatch.setenv("KB_POOL_MIN_SIZE", "1")
    for kwargs, want in [({}, (1, 4)), ({"max_size": 7}, (1, 7)),
                         ({"max_size": 4, "min_size": 2}, (2, 4))]:   # explicit min wins
        pool = open_pool(TEST_DATABASE_URL, **kwargs)
        try:
            assert (pool.min_size, pool.max_size) == want, kwargs
        finally:
            pool.close()
    monkeypatch.setenv("KB_POOL_MIN_SIZE", "9")                        # never above the ceiling
    pool = open_pool(TEST_DATABASE_URL, max_size=6)
    try:
        assert (pool.min_size, pool.max_size) == (6, 6)
    finally:
        pool.close()


@pytest.mark.parametrize("bad", ["x", "-1", "1.5"])
def test_open_pool_rejects_a_malformed_KB_POOL_MIN_SIZE(monkeypatch, bad):
    monkeypatch.setenv("KB_POOL_MIN_SIZE", bad)
    with pytest.raises(ValueError, match="KB_POOL_MIN_SIZE"):
        open_pool(TEST_DATABASE_URL)


def test_open_pool_accepts_a_max_size_below_the_default_min_size():
    """psycopg_pool's min_size defaults to 4 and it rejects max_size < min_size, so a
    small ceiling (a tenant pool with KB_TENANT_POOL_MAX=2) must pull min_size down
    with it rather than raise."""
    pool = open_pool(TEST_DATABASE_URL, max_size=2)
    try:
        assert pool.max_size == 2
        assert pool.min_size == 2
        with pool.connection() as conn:
            assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
    finally:
        pool.close()


def test_open_pool_replaces_a_connection_the_server_killed_while_idle():
    """A Postgres restart (e.g. recreating the database container) kills
    every pooled connection server-side while it sits idle. Without a checkout check the
    pool hands those dead connections out: the dashboard served 500s ("server closed the
    connection unexpectedly"), then 30 s PoolTimeouts on every request until a restart.
    The next checkout after the kill must get a live connection instead."""
    pool = open_pool(TEST_DATABASE_URL, max_size=1)
    try:
        with pool.connection() as conn:
            pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
            for _ in range(50):  # termination is asynchronous; wait until it has landed
                if admin.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s",
                                 (pid,)).fetchone() is None:
                    break
                time.sleep(0.05)
        with pool.connection() as conn:
            assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
    finally:
        pool.close()


def _fresh_database(name: str) -> str:
    """A brand-new, empty database next to the test one (template0: no stale schema)."""
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    admin = make_conninfo(TEST_DATABASE_URL, dbname="postgres")
    try:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))
            c.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)))
    except psycopg.Error as exc:
        pytest.skip(f"cannot create a scratch database here: {exc}")
    url = make_conninfo(TEST_DATABASE_URL, dbname=name)
    try:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS ltree")
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except psycopg.Error as exc:
        pytest.skip(f"cannot create extensions in the scratch database: {exc}")
    assert conninfo_to_dict(url)["dbname"] == name
    return url


def _drop_database(name: str) -> None:
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    with psycopg.connect(make_conninfo(TEST_DATABASE_URL, dbname="postgres"),
                         autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(name)))


def test_concurrent_first_boot_migrations_do_not_race():
    """kb-run starts every daemon at once and each migrates at startup: on a fresh
    database they used to race to CREATE schema_migrations and crash with
    UniqueViolation. Under the advisory lock exactly one applies everything."""
    import threading

    from kb_core.db import MIGRATIONS_DIR, migrate
    from psycopg.rows import dict_row

    name = "kb_mcp_test_migrate_race"
    url = _fresh_database(name)
    try:
        results, errors = [], []
        barrier = threading.Barrier(4)

        def boot():
            try:
                with psycopg.connect(url, row_factory=dict_row) as conn:
                    barrier.wait()
                    results.append(migrate(conn))
            except Exception as exc:          # noqa: BLE001 — collected for the assert
                errors.append(exc)

        threads = [threading.Thread(target=boot) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        assert errors == []
        every = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
        assert sorted(len(r) for r in results) == [0, 0, 0, len(every)]
        with psycopg.connect(url) as conn:
            held = conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                                ).fetchone()[0]
        assert held == 0                         # every session released the lock
    finally:
        _drop_database(name)


def test_migrate_holds_its_lock_until_the_caller_commits(pool):
    """The lock must outlive the schema writes: released before the commit, the next
    daemon could take it and still not see the committed schema."""
    from kb_core.db import _MIGRATE_LOCK, migrate
    with pool.connection() as conn, pool.connection() as other:
        other.autocommit = True
        try:
            migrate(conn)                                  # nothing to apply; tx still open
            probe = "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok"
            assert other.execute(probe, (_MIGRATE_LOCK,)).fetchone()["ok"] is False
            conn.commit()
            assert other.execute(probe, (_MIGRATE_LOCK,)).fetchone()["ok"] is True
            other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_MIGRATE_LOCK,))
        finally:
            other.autocommit = False      # never hand an autocommit connection back to the pool


def test_migrate_on_an_autocommit_connection_releases_its_session_lock(pool):
    from kb_core.db import _MIGRATE_LOCK, migrate
    with pool.connection() as conn, pool.connection() as other:
        conn.autocommit = True
        other.autocommit = True
        try:
            assert migrate(conn) == []
            ok = other.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                               (_MIGRATE_LOCK,)).fetchone()["ok"]
            assert ok is True
            other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_MIGRATE_LOCK,))
        finally:
            conn.autocommit = False
            other.autocommit = False
