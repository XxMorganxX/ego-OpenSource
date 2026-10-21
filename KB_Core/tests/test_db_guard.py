"""The test-database guard: the fixtures TRUNCATE their database, so a DSN must name a
test database and must not be the live KB_DATABASE_URL."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kb_core.db import disposable_db_refusal

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("url", [
    "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test",            # local default
    "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test_retrieval",  # KB_Retrieval
    "postgresql://kb:kb@localhost:5432/kb_mcp_test",                      # CI
    "postgresql://kb:kb@localhost:5432/kb_mcp_test_b",                    # CI second tenant
    "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test2",           # URL_B default
    "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test_stamp",      # scratch
    "postgresql://u:p@127.0.0.1:1/kb_verify_x_b?sslmode=disable",          # kb-publish verify
    "postgresql://u:p@127.0.0.1:1/kb_verify",
])
def test_accepts_the_shipped_and_ci_test_databases(url):
    assert disposable_db_refusal(url, env={}) is None


@pytest.mark.parametrize("name", ["kb_mcp", "kb_t_alice", "postgres", "notes", "contest",
                                  "kb_mcp_testing", ""])
def test_refuses_a_database_not_named_as_a_test_database(name):
    msg = disposable_db_refusal(f"postgresql://u:p@localhost:5433/{name}", env={})
    assert msg and "TRUNCATE" in msg


def test_refuses_the_live_database_even_when_named_like_a_test_one():
    live = "postgresql://kb_app:pw@localhost:5433/kb_test"
    same = "postgresql://kb_test:kb_test@127.0.0.1:5433/kb_test"      # other role, same DB
    assert "KB_DATABASE_URL" in disposable_db_refusal(same, env={"KB_DATABASE_URL": live})
    other_port = "postgresql://kb_test:kb_test@127.0.0.1:55501/kb_test"
    assert disposable_db_refusal(other_port, env={"KB_DATABASE_URL": live}) is None


def test_explicit_override_accepts_any_database():
    url = "postgresql://u:p@localhost:5433/scratch"
    assert disposable_db_refusal(url, env={}) is not None
    assert disposable_db_refusal(url, env={"KB_TEST_ALLOW_ANY_DATABASE": "1"}) is None


def test_pytest_refuses_to_start_against_a_non_test_database(tmp_path):
    """End to end through conftest.py: the session stops with a usage error before any
    fixture could connect (port 1 would refuse a connection anyway)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("KB_TEST_DATABASE_URL_B", "KB_TEST_ALLOW_ANY_DATABASE")}
    env["KB_TEST_DATABASE_URL"] = "postgresql://kb_app:pw@127.0.0.1:1/kb_mcp"
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "tests/test_scheduler.py"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == pytest.ExitCode.USAGE_ERROR, res.stdout + res.stderr
    assert "refusing to run the test suite against database 'kb_mcp'" in res.stderr
