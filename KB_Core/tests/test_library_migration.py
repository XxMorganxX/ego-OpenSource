"""Migration 0053: a `kind` on every registry table, per-revision review state, and
the backfill that approves each existing item's current revision."""
from pathlib import Path

import psycopg
import pytest

from kb_core import skills


def _md(name, body="v1\n"):
    return f"---\nname: {name}\ndescription: Example for tests.\n---\n\n{body}"


def test_0053_keys_every_table_by_kind(kb):
    with kb._pool.connection() as c:
        pk = {r["t"]: r["cols"] for r in c.execute(
            "SELECT tc.table_name AS t, string_agg(k.column_name, ',' ORDER BY k.ordinal_position) AS cols "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage k USING (constraint_name, table_name) "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            "AND tc.table_name IN ('skills','skill_revisions','skill_revision_files') "
            "GROUP BY tc.table_name")}
    assert pk == {"skills": "kind,name",
                  "skill_revisions": "kind,name,revision",
                  "skill_revision_files": "kind,name,revision,path"}


def test_0053_lets_a_skill_and_an_agent_share_a_name_and_checks_kind(kb):
    with kb._pool.connection() as c:
        for kind in ("skill", "agent"):
            c.execute("INSERT INTO skills (kind, name, current_revision) VALUES (%s, 'shared', 1)", (kind,))
            c.execute("INSERT INTO skill_revisions (kind, name, revision, description, change_summary, "
                      "package_hash) VALUES (%s, 'shared', 1, 'd', 'c', 'h')", (kind,))
        rows = c.execute("SELECT kind, review_status FROM skill_revisions WHERE name = 'shared' "
                         "ORDER BY kind").fetchall()
        assert [(r["kind"], r["review_status"]) for r in rows] == [("agent", "pending"), ("skill", "pending")]
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO skills (kind, name, current_revision) VALUES ('tool', 'x', 1)")


def test_0053_rejects_an_unknown_review_status(kb):
    kb.upsert_skill("review-infra", None, {"SKILL.md": _md("review-infra")}, "create")
    with kb._pool.connection() as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("UPDATE skill_revisions SET review_status = 'maybe' WHERE name = 'review-infra'")


def test_0053_backfill_approves_each_current_revision_and_supersedes_older(kb):
    """Replays the backfill statements on rows written as 0051 would have left them."""
    with kb._pool.connection() as c:
        c.execute("INSERT INTO skills (kind, name, current_revision) VALUES ('skill', 'old-one', 2)")
        for rev in (1, 2):
            c.execute("INSERT INTO skill_revisions (kind, name, revision, description, change_summary, "
                      "package_hash) VALUES ('skill', 'old-one', %s, 'd', 'c', %s)", (rev, f"h{rev}"))
        sql = (Path(skills.__file__).parent / "migrations/0053_library_kinds_review.sql").read_text()
        backfill = sql.split("-- BACKFILL", 1)[1]
        c.execute(backfill)
        rows = c.execute("SELECT revision, review_status, reviewer_label, reviewed_hash "
                         "FROM skill_revisions WHERE name = 'old-one' ORDER BY revision").fetchall()
        assert [(r["revision"], r["review_status"]) for r in rows] == [(1, "superseded"), (2, "approved")]
        assert rows[1]["reviewer_label"] == "legacy-import" and rows[1]["reviewed_hash"] == "h2"
        assert c.execute("SELECT approved_revision FROM skills WHERE name = 'old-one'"
                         ).fetchone()["approved_revision"] == 2
