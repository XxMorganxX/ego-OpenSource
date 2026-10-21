"""Create the two test databases scripts/ci/python.sh needs, if missing.

Reads KB_TEST_DATABASE_URL and KB_TEST_DATABASE_URL_B. A missing database is
created from template0 (template1 may carry a stale schema from an earlier
run), through the server's `postgres` maintenance database. Each one then gets
the ltree + vector extensions the migrations expect.
"""
import os

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def ensure(url: str) -> None:
    name = conninfo_to_dict(url).get("dbname")
    if not name:
        raise SystemExit(f"prepare_test_dbs: no database name in {url!r}")
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            c.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)))
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS ltree")
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")


if __name__ == "__main__":
    for var in ("KB_TEST_DATABASE_URL", "KB_TEST_DATABASE_URL_B"):
        ensure(os.environ[var])
    print("prepare_test_dbs: databases + extensions ready")
