"""Connection pool helper and a minimal ordered-SQL migration runner."""

import os
import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_DEFAULT_SIZE = 4          # psycopg_pool's default min_size (and so the default max_size)
POOL_MIN_ENV = "KB_POOL_MIN_SIZE"


def _env_min_size() -> "int | None":
    """KB_POOL_MIN_SIZE as a non-negative int, None when unset/empty. kb-svc.sh pins it to 1
    for tenant daemons: four daemons x a floor of 4 per tenant was the
    dominant term in the max_connections arithmetic."""
    raw = os.environ.get(POOL_MIN_ENV, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ValueError(f"{POOL_MIN_ENV} must be a non-negative integer, got {raw!r}")
    return int(raw)


def open_pool(conninfo: str, *, max_size: "int | None" = None,
              min_size: "int | None" = None) -> ConnectionPool:
    """A connection pool whose connections return dict rows; a `max_size` given without a
    `min_size` pulls `min_size` down to `min(4, max_size)`, since psycopg_pool rejects a
    ceiling below its default floor of 4.

    `max_size=None` is forwarded verbatim and is ConnectionPool's own default, which
    psycopg_pool resolves to `min_size` (`BasePool._check_size`: "if max_size is
    None: max_size = min_size") -- i.e. 4. So this keyword is strictly
    additive: every caller that does not pass it keeps the pool it already had.

    Pass a ceiling when the caller can have several blocking reads genuinely in
    flight at once. KB_MCP does, as of the async-tool conversion: before it, FastMCP
    ran every sync tool body inline on the event loop, so tool calls were serialised
    and pool concurrency was never exercised beyond one connection. After it, tool
    bodies run in worker threads and a `cluster-crawl` holds ONE connection pinned in
    REPEATABLE READ (`KnowledgeBase.read_snapshot`) for its whole duration, so the
    pool -- not the event loop -- is the concurrency limit.

    On exhaustion `pool.connection()` BLOCKS, it does not fail fast: `getconn` waits
    up to `timeout` (default 30.0 s) and then raises
    `psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")`
    (`ConnectionPool.getconn`). `max_waiting` defaults to 0, which means *unbounded*
    waiters (`ConnectionPool._get_ready_connection` only raises `TooManyRequests` when
    `max_waiting` is truthy), so nothing is rejected up front -- callers queue, and
    each one's wait is bounded by that 30 s.

    KB_POOL_MIN_SIZE (env), when set and no `min_size` is passed, lowers the idle floor
    without moving the ceiling (clamped to it). Unset -- the owner's case -- changes nothing.

    Every checkout is pinged (`check=ConnectionPool.check_connection`, one round trip) and
    a dead connection is replaced, not handed out. Without it a Postgres restart left
    every idle pooled connection dead: a database container recreate once gave the
    dashboard 500s and then 30 s PoolTimeouts on every request until it was restarted.
    """
    if min_size is None:
        floor = _env_min_size()
        if floor is not None:
            # KB_POOL_MIN_SIZE lowers the idle floor only: the ceiling stays what this
            # caller had (its max_size, else psycopg_pool's default of 4).
            max_size = max_size if max_size is not None else _DEFAULT_SIZE
            min_size = min(floor, max_size)
    if min_size is None and max_size is not None:
        min_size = min(_DEFAULT_SIZE, max_size)
    # Both None: forward nothing extra, so psycopg_pool's own defaults apply unchanged.
    sizing = {} if min_size is None else {"min_size": min_size}
    return ConnectionPool(conninfo, kwargs={"row_factory": dict_row},
                          max_size=max_size, open=True,
                          check=ConnectionPool.check_connection, **sizing)


# --- test-database guard ------------------------------------------------------------
# The suites' fixtures TRUNCATE their database. A DSN is accepted only if its database
# name says it is disposable: a `test` token (kb_mcp_test, kb_mcp_test_retrieval, the
# `_b` / `2` second databases, CI's kb_mcp_test) or kb-publish verify's kb_verify[_x].
_DISPOSABLE_DB = re.compile(r"(?:^|_)test(?:_|\d|$)|^kb_verify(?:_[a-z0-9_]+)?$")
TEST_DB_GUARD_OVERRIDE = "KB_TEST_ALLOW_ANY_DATABASE"


def _db_identity(url: str) -> "tuple[str, str, str]":
    from psycopg.conninfo import conninfo_to_dict
    info = conninfo_to_dict(url)
    host = (info.get("host") or "localhost").lower()
    host = "127.0.0.1" if host in ("localhost", "::1") else host
    return host, str(info.get("port") or "5432"), info.get("dbname") or ""


def disposable_db_refusal(url: str, env: "dict | None" = None) -> "str | None":
    """Why `url` must not be handed to a test fixture that TRUNCATEs, or None if it may.

    Refuses a database whose name doesn't mark it as a test database, and one that is
    the same database as KB_DATABASE_URL (the live KB). KB_TEST_ALLOW_ANY_DATABASE=1 is the
    explicit opt-in for a scratch database named otherwise."""
    env = os.environ if env is None else env
    if (env.get(TEST_DB_GUARD_OVERRIDE) or "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    try:
        ident = _db_identity(url)
    except Exception as exc:
        return f"cannot parse the test DSN ({type(exc).__name__}): refusing to TRUNCATE it"
    name = ident[2]
    if not _DISPOSABLE_DB.search(name):
        return (f"refusing to run the test suite against database {name or '<none>'!r}: "
                "the fixtures TRUNCATE it, and its name doesn't mark it as a test "
                "database (it must contain a `test` token, e.g. kb_mcp_test). Point "
                f"KB_TEST_DATABASE_URL at a disposable database, or set "
                f"{TEST_DB_GUARD_OVERRIDE}=1 if this one really is scratch.")
    live = env.get("KB_DATABASE_URL")
    if live:
        try:
            same = _db_identity(live) == ident
        except Exception:
            same = False
        if same:
            return (f"refusing to run the test suite against {name!r}: it is the database "
                    "KB_DATABASE_URL points at (your live KB), and the fixtures TRUNCATE it.")
    return None


def _applied_versions(conn: psycopg.Connection) -> set[str]:
    # A catalog probe, not "SELECT and catch UndefinedTable": the rollback that catch
    # needed would also drop migrate()'s transaction-scoped lock (and is forbidden
    # inside a caller's conn.transaction() block).
    with conn.cursor(row_factory=tuple_row) as cur:
        if not cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL"
                           ).fetchone()[0]:
            return set()
        return {v for (v,) in cur.execute("SELECT version FROM schema_migrations")}


_MIGRATE_LOCK = "kb_core_migrate"     # advisory-lock key (hashtext) for migrate()


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any unapplied *.sql migrations in order. Returns versions applied.

    The version key is the file's full stem, not its numeric prefix, so two files
    may share a number: 0048_llm_jobs and 0048_mcp_trace_turns both exist because two
    branches each took 0048 before merging. That is intentional and harmless (both
    apply, in name order) -- and the files must never be renamed, since a rename
    changes a version key every existing database has already recorded. New
    migrations take a fresh number; test_migration_prefixes refuses a new duplicate.

    Serialised by an advisory lock: every daemon migrates at startup and kb-run starts
    them all at once, so on a fresh database they raced to create schema_migrations and
    crashed with UniqueViolation (pg_type_typname_nsp_index). Whoever gets the lock
    second sees the work already applied.

    On a normal (non-autocommit) connection the lock is TRANSACTION-scoped and the
    migrations run inside that one transaction, so the lock is released only when the
    caller commits — after the schema is durable, never before. Commit is the caller's,
    as it always was: `migrate_pool` and `with psycopg.connect(...)` commit on exit, and
    a caller inside its own conn.transaction() block keeps control of it. On an
    autocommit connection each migration commits on its own and a session lock is held
    for the duration instead.
    """
    if conn.autocommit:
        conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (_MIGRATE_LOCK,))
    else:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_MIGRATE_LOCK,))
    try:
        applied = _applied_versions(conn)
        newly: list[str] = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text()
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
            newly.append(version)
        return newly
    finally:
        if conn.autocommit:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_MIGRATE_LOCK,))


def migrate_pool(pool: ConnectionPool) -> list[str]:
    """Run migrations on a fresh pooled connection."""
    with pool.connection() as conn:
        return migrate(conn)
