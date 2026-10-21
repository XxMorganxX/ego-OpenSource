import os

from kb_core.knowledge_base import TURN_GAP_S
from kb_core.turns_rebuild import main

# Duplicated from tests/conftest.py rather than imported -- the house convention
# (see tests/test_db.py).
TEST_DATABASE_URL = os.environ.get(
    "KB_TEST_DATABASE_URL", "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test"
)


def test_cli_reports_the_gap_and_the_moved_count(kb, capsys):
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=5, session_id="s1")
    assert main(["--database-url", TEST_DATABASE_URL]) == 0
    out = capsys.readouterr().out
    assert f"gap {TURN_GAP_S}s" in out and "0 row(s) moved" in out


def test_cli_reassigns_a_misassigned_row_and_reports_it(kb, capsys):
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=5)
    kb.log_trace(tool="get_memory", args={"id": "y"}, latency_ms=5)
    one, other = (r["id"] for r in kb.trace_recent())      # both sessionless
    with kb._pool.connection() as conn:
        conn.execute("UPDATE mcp_trace SET turn_id = %(other)s WHERE id = %(one)s",
                     {"one": one, "other": other})
    assert main(["--database-url", TEST_DATABASE_URL]) == 0
    assert "1 row(s) moved" in capsys.readouterr().out
    assert {r["id"]: r["turn_id"] for r in kb.trace_recent()}[one] == one
