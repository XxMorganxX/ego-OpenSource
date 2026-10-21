"""The skill registry's engine behaviour against a live database."""
import json
import threading

import psycopg
import pytest

from kb_core import skills
from kb_core.skills import (FileNotFound, InvalidArgument, InvalidPackage, PackageTooLarge,
                            RevisionConflict, RevisionNotFound, SkillNotFound)


def _md(name, desc="Review infrastructure changes.", body="# Body\n"):
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


def _pkg(name, body="v1\n", extra=None):
    files = {"SKILL.md": _md(name, body=body)}
    files.update(extra or {})
    return files


def test_0051_creates_the_registry_tables_with_cascading_revisions(kb):
    with kb._pool.connection() as c:
        tables = {r["tablename"] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename LIKE 'skill%'")}
        assert tables == {"skills", "skill_revisions", "skill_revision_files"}
        assert c.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'skills_name_trgm_idx'"
                         ).fetchone() is not None
        c.execute("INSERT INTO skills (name, current_revision) VALUES ('t-skill', 1)")
        c.execute("INSERT INTO skill_revisions (name, revision, description, change_summary, "
                  "package_hash) VALUES ('t-skill', 1, 'd', 'c', 'h')")
        c.execute("INSERT INTO skill_revision_files (name, revision, path, content, "
                  "content_hash) VALUES ('t-skill', 1, 'SKILL.md', 'x', 'h')")
        c.execute("DELETE FROM skills WHERE name = 't-skill'")
        assert c.execute("SELECT count(*) AS n FROM skill_revisions").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM skill_revision_files").fetchone()["n"] == 0


def test_create_then_update_advances_the_revision_and_keeps_history(kb):
    out = kb.upsert_skill("review-infra", None, _pkg("review-infra"), "create",
                          actor="m@x.test", agent_label="claude-code")
    assert out["revision"] == 1 and out["archived"] is False and out["reactivated"] is False
    assert out["description"] == "Review infrastructure changes."
    out2 = kb.upsert_skill("review-infra", 1,
                           _pkg("review-infra", body="v2\n", extra={"references/notes.md": "n\n"}),
                           "add notes")
    assert out2["revision"] == 2 and out2["package_hash"] != out["package_hash"]
    cur = kb.get_skill("review-infra")
    assert cur["revision"] == 2 == cur["current_revision"] and cur["archived"] is False
    assert [f["path"] for f in cur["files"]] == ["SKILL.md", "references/notes.md"]
    assert cur["files"][1] == {"path": "references/notes.md", "bytes": 2,
                               "content_hash": cur["files"][1]["content_hash"]}
    assert cur["skill_md"].endswith("v2\n") and cur["package_hash"] == out2["package_hash"]
    assert [h["revision"] for h in cur["history"]] == [2, 1]
    assert cur["history"][1]["actor"] == "m@x.test"
    assert cur["history"][1]["agent_label"] == "claude-code"
    assert cur["history"][0]["actor"] is None and cur["history"][0]["agent_label"] is None
    assert cur["history"][0]["change_summary"] == "add notes"
    assert cur["history"][0]["created_at"] >= cur["history"][1]["created_at"]
    old = kb.get_skill("review-infra", revision=1)
    assert old["revision"] == 1 and old["current_revision"] == 2
    assert old["skill_md"].endswith("v1\n") and [f["path"] for f in old["files"]] == ["SKILL.md"]
    assert old["package_hash"] == out["package_hash"]


def test_numbered_expected_revision_still_compares_and_swaps(kb):
    kb.upsert_skill("a-skill", None, _pkg("a-skill"), "create")
    with pytest.raises(SkillNotFound):
        kb.upsert_skill("b-skill", 1, _pkg("b-skill"), "update missing")
    with pytest.raises(RevisionConflict) as exc:
        kb.upsert_skill("a-skill", 7, _pkg("a-skill"), "stale")
    assert exc.value.current_revision == 1
    with pytest.raises(InvalidArgument):
        kb.upsert_skill("a-skill", 0, _pkg("a-skill"), "zero")
    with pytest.raises(InvalidArgument):
        kb.upsert_skill("a-skill", True, _pkg("a-skill"), "bool is not a revision")
    cur = kb.get_skill("a-skill")
    assert cur["current_revision"] == 1 and len(cur["history"]) == 1


def test_invalid_package_leaves_history_and_pointer_untouched(kb):
    kb.upsert_skill("a-skill", None, _pkg("a-skill"), "create")
    with pytest.raises(InvalidPackage):
        kb.upsert_skill("a-skill", 1, {"SKILL.md": _md("a-skill"), "../x": "y"}, "bad")
    with pytest.raises(InvalidPackage):
        kb.upsert_skill("a-skill", 1, {"SKILL.md": _md("wrong-name")}, "bad")
    with pytest.raises(InvalidPackage, match="change_summary"):
        kb.upsert_skill("a-skill", 1, _pkg("a-skill", body="v2\n"), "   ")
    with pytest.raises(InvalidArgument, match="agent_label"):
        kb.upsert_skill("a-skill", 1, _pkg("a-skill", body="v2\n"), "ok", agent_label="x" * 129)
    cur = kb.get_skill("a-skill")
    assert cur["current_revision"] == 1 and len(cur["history"]) == 1
    with kb._pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM skill_revision_files").fetchone()["n"] == 1


def test_nul_in_change_summary_or_agent_label_is_a_stable_error_not_a_db_error(kb):
    kb.upsert_skill("a-skill", None, _pkg("a-skill"), "create")
    with pytest.raises(InvalidPackage, match="NUL"):
        kb.upsert_skill("a-skill", 1, _pkg("a-skill", body="v2\n"), "fix\x00")
    with pytest.raises(InvalidArgument, match="NUL"):
        kb.upsert_skill("a-skill", 1, _pkg("a-skill", body="v2\n"), "ok", agent_label="x\x00")
    with pytest.raises(InvalidPackage, match="change_summary is not valid UTF-8"):
        kb.upsert_skill("a-skill", 1, _pkg("a-skill", body="v2\n"), "fix\ud800")
    with pytest.raises(InvalidArgument, match="agent_label is not valid UTF-8"):
        kb.upsert_skill("a-skill", 1, _pkg("a-skill", body="v2\n"), "ok", agent_label="x\ud800")
    assert kb.get_skill("a-skill")["current_revision"] == 1


def test_change_summary_is_bounded(kb):
    kb.upsert_skill("a-skill", None, _pkg("a-skill"), "create")
    with pytest.raises(PackageTooLarge, match="change_summary"):
        kb.upsert_skill("a-skill", 1, _pkg("a-skill", body="v2\n"), "x" * 2001)
    assert kb.get_skill("a-skill")["current_revision"] == 1
    out = kb.upsert_skill("a-skill", 1, _pkg("a-skill", body="v2\n"), "x" * 2000)
    assert out["revision"] == 2


def test_get_reports_misses_by_category_and_caps_history(kb):
    with pytest.raises(SkillNotFound):
        kb.get_skill("nope")
    with pytest.raises(InvalidPackage):
        kb.get_skill("Not A Name")
    kb.upsert_skill("busy", None, _pkg("busy"), "create")
    for i in range(1, skills.HISTORY_LIMIT + 3):
        kb.upsert_skill("busy", i, _pkg("busy", body=f"v{i + 1}\n"), f"rev {i + 1}")
    cur = kb.get_skill("busy")
    assert cur["current_revision"] == skills.HISTORY_LIMIT + 3
    assert len(cur["history"]) == skills.HISTORY_LIMIT
    assert cur["history"][0]["revision"] == skills.HISTORY_LIMIT + 3
    assert kb.get_skill("busy", revision=1)["skill_md"].endswith("v1\n")   # old ones still readable
    with pytest.raises(RevisionNotFound):
        kb.get_skill("busy", revision=99)
    with pytest.raises(InvalidArgument):
        kb.get_skill("busy", revision=0)


def _race(kb, expected_revision):
    results, barrier = [], threading.Barrier(2)

    def go(tag):
        barrier.wait()
        try:
            out = kb.upsert_skill("race", expected_revision, _pkg("race", body=f"{tag}\n"), tag)
            results.append(("ok", out["revision"]))
        except RevisionConflict as e:
            results.append(("conflict", e.current_revision))

    threads = [threading.Thread(target=go, args=(t,)) for t in ("left", "right")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return sorted(results)


def test_concurrent_updates_against_one_revision_yield_one_success_one_conflict(kb):
    kb.upsert_skill("race", None, _pkg("race"), "create")
    assert _race(kb, 1) == [("conflict", 2), ("ok", 2)]
    assert [h["revision"] for h in kb.get_skill("race")["history"]] == [2, 1]


def test_concurrent_creates_both_land(kb):
    assert _race(kb, None) == [("ok", 1), ("ok", 2)]
    assert kb.get_skill("race")["current_revision"] == 2


def test_archive_keeps_reads_is_idempotent_and_reactivates_on_approval(kb):
    kb.upsert_skill("old-way", None, _pkg("old-way"), "create", actor="m@x.test")
    with pytest.raises(RevisionConflict) as exc:
        kb.delete_skill("old-way", 5)
    assert exc.value.current_revision == 1
    with pytest.raises(SkillNotFound):
        kb.delete_skill("never", 1)
    with pytest.raises(InvalidArgument):
        kb.delete_skill("old-way", 0)
    out = kb.delete_skill("old-way", 1, actor="m@x.test")
    assert out == {"name": "old-way", "revision": 1, "archived": True}
    assert kb.delete_skill("old-way", 1, actor="m@x.test")["archived"] is True   # idempotent
    got = kb.get_skill("old-way")
    assert got["archived"] is True and got["archived_by"] == "m@x.test"
    assert got["archived_at"] is not None and got["current_revision"] == 1
    assert got["skill_md"].endswith("v1\n")                                        # still readable
    # A write stays archived until approved; approval restores it.
    back0 = kb.upsert_skill("old-way", None, _pkg("old-way", body="v2\n"), "recreate")
    assert back0["revision"] == 2 and back0["reactivated"] is False and back0["archived"] is True
    assert kb.get_skill("old-way")["archived"] is True
    assert kb.review_skill("old-way", 2, back0["package_hash"], True, "read")["reactivated"] is True
    kb.delete_skill("old-way")
    back = kb.upsert_skill("old-way", 2, _pkg("old-way", body="v3\n"), "restore")
    assert back["revision"] == 3 and back["archived"] is True and back["reactivated"] is False
    done = kb.review_skill("old-way", 3, back["package_hash"], True, "read")
    assert done["reactivated"] is True                                             # visibly
    got = kb.get_skill("old-way")
    assert got["archived"] is False and got["archived_at"] is None and got["archived_by"] is None
    plain = kb.upsert_skill("old-way", 3, _pkg("old-way", body="v4\n"), "plain update")
    assert plain["reactivated"] is False


def test_get_file_reads_at_an_explicit_revision_and_distinguishes_misses(kb):
    kb.upsert_skill("with-refs", None,
                    _pkg("with-refs", extra={"references/a.md": "A1\n",
                                             "scripts/run.sh": "#!/bin/sh\necho hi\n"}),
                    "create")
    kb.upsert_skill("with-refs", 1, _pkg("with-refs", extra={"references/a.md": "A2\n"}),
                    "drop the script")
    f = kb.get_skill_file("with-refs", "references/a.md", 1)
    assert f == {"name": "with-refs", "revision": 1, "path": "references/a.md",
                 "content": "A1\n", "content_hash": f["content_hash"]}
    assert len(f["content_hash"]) == 64
    assert kb.get_skill_file("with-refs", "references/a.md", 2)["content"] == "A2\n"
    # Script source is served as text; nothing here executes it.
    assert kb.get_skill_file("with-refs", "scripts/run.sh", 1)["content"].startswith("#!/bin/sh")
    with pytest.raises(FileNotFound):
        kb.get_skill_file("with-refs", "scripts/run.sh", 2)
    with pytest.raises(RevisionNotFound):
        kb.get_skill_file("with-refs", "SKILL.md", 9)
    with pytest.raises(SkillNotFound):
        kb.get_skill_file("nope", "SKILL.md", 1)
    with pytest.raises(InvalidArgument):
        kb.get_skill_file("with-refs", "SKILL.md", None)
    with pytest.raises(InvalidPackage):
        kb.get_skill_file("with-refs", "../SKILL.md", 1)


def test_search_browses_alphabetically_and_ranks_a_query_lexically(kb):
    for name, desc in [("deploy-check", "Verify a deployment after release."),
                       ("review-infra", "Review infrastructure changes and verify deployments."),
                       ("write-tests", "Write unit tests for a change.")]:
        kb.upsert_skill(name, None, {"SKILL.md": _md(name, desc=desc)}, "create")
    browse = kb.search_skills()
    assert [s["name"] for s in browse["skills"]] == ["deploy-check", "review-infra", "write-tests"]
    assert browse["next_cursor"] is None and browse["query"] is None
    row = browse["skills"][0]
    assert row["description"] == "Verify a deployment after release."
    assert row["revision"] == 1 and row["archived"] is False and row["updated_at"] is not None
    assert {"name", "description", "revision", "updated_at", "archived"} <= set(row)
    hits = kb.search_skills("deployment")["skills"]
    assert {s["name"] for s in hits} == {"deploy-check", "review-infra"}
    assert kb.search_skills("deploy")["skills"][0]["name"] == "deploy-check"   # name beats description
    assert kb.search_skills("unit tests")["skills"][0]["name"] == "write-tests"
    assert kb.search_skills("zzzz-nothing")["skills"] == []
    assert kb.search_skills("  deploy  ")["query"] == "deploy"


@pytest.mark.parametrize("q", ["c++ & (x", "'; drop table skills; --", "%_\\", "the", "   ",
                               "a" * 500])
def test_search_never_raises_on_odd_query_text(kb, q):
    kb.upsert_skill("deploy-check", None,
                    {"SKILL.md": _md("deploy-check", desc="Verify the deployment.")}, "create")
    out = kb.search_skills(q)
    assert isinstance(out["skills"], list)


@pytest.mark.parametrize("q", ["a\x00b", "a\ud800b"])
def test_search_rejects_nul_and_surrogates(kb, q):
    with pytest.raises(InvalidArgument, match="query"):
        kb.search_skills(q)


def test_search_hides_archived_unless_asked(kb):
    kb.upsert_skill("old-way", None, _pkg("old-way"), "create")
    kb.upsert_skill("new-way", None, _pkg("new-way"), "create")
    kb.delete_skill("old-way", 1)
    assert [s["name"] for s in kb.search_skills()["skills"]] == ["new-way"]
    both = kb.search_skills(include_archived=True)["skills"]
    assert [(s["name"], s["archived"]) for s in both] == [("new-way", False), ("old-way", True)]
    assert kb.search_skills("old")["skills"] == []
    assert [s["name"] for s in kb.search_skills("old", include_archived=True)["skills"]] == ["old-way"]


def test_search_pages_with_an_opaque_cursor(kb):
    for i in range(7):
        kb.upsert_skill(f"skill-{i:02d}", None, {"SKILL.md": _md(f"skill-{i:02d}")}, "create")
    page1 = kb.search_skills(page_size=3)
    assert [s["name"] for s in page1["skills"]] == ["skill-00", "skill-01", "skill-02"]
    assert page1["next_cursor"]
    page2 = kb.search_skills(cursor=page1["next_cursor"], page_size=3)
    assert [s["name"] for s in page2["skills"]] == ["skill-03", "skill-04", "skill-05"]
    page3 = kb.search_skills(cursor=page2["next_cursor"], page_size=3)
    assert [s["name"] for s in page3["skills"]] == ["skill-06"] and page3["next_cursor"] is None
    with pytest.raises(InvalidArgument):
        kb.search_skills(cursor="not-a-cursor")
    assert len(kb.search_skills(page_size=10_000)["skills"]) == 7     # page_size is clamped


def test_0052_skill_writes_notify_kb_change_with_the_skill_name(kb, pool):
    """skills has no `id` column, so the shared kb_notify_change() cannot be attached
    (it reads rec.id and would raise inside upsert_skill); 0052's own function sends
    the name instead. One notification per create, update and archive."""
    with psycopg.connect(pool.conninfo, autocommit=True) as listen:
        listen.execute("LISTEN kb_change")
        kb.upsert_skill("deploy-checklist", None, _pkg("deploy-checklist"), "create")
        kb.upsert_skill("deploy-checklist", 1, _pkg("deploy-checklist", body="v2\n"), "update")
        kb.delete_skill("deploy-checklist", 2)
        got = [json.loads(n.payload) for n in listen.notifies(timeout=5, stop_after=3)]
    assert [(p["table"], p["op"], p["id"]) for p in got] == [
        ("skills", "INSERT", "deploy-checklist"),
        ("skills", "UPDATE", "deploy-checklist"),
        ("skills", "UPDATE", "deploy-checklist"),
    ]
