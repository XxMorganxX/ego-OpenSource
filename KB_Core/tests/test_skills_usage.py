"""Skill usage over the MCP trace log (dashboard Skills page). Read-only aggregates:
loads, file reads, search surfacing, misses, requests for missing skills."""
from datetime import timedelta

import pytest

from kb_core.skills import InvalidArgument, InvalidPackage


def _md(name, desc="Example skill for tests.", body="Body\n"):
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


def _skill(kb, name):
    kb.upsert_skill(name, None, {"SKILL.md": _md(name)}, "create")


def _log(kb, pool, tool, *, name=None, query=None, result_count=0, trace=None,
         error=None, caller=None, args=None, ago=None, session_id=None):
    """One trace row shaped like KB_MCP writes it; `ago` backdates it."""
    a = dict(args or {})
    if name is not None:
        a["name"] = name
    if tool == "search_skills":
        a.setdefault("query", query)
        a.setdefault("cursor", None)
    kb.log_trace(tool=tool, args=a, latency_ms=3, query=query, result_count=result_count,
                 trace=trace, error=error, caller=caller, session_id=session_id)
    if ago is not None:
        with pool.connection() as c:
            c.execute("UPDATE mcp_trace SET at = now() - %s "
                      "WHERE id = (SELECT max(id) FROM mcp_trace)", (ago,))


def test_usage_for_counts_loads_file_reads_and_surfacing(kb, pool):
    _skill(kb, "deploy-checklist")
    _skill(kb, "review-infra")
    _log(kb, pool, "get_skill", name="deploy-checklist", trace={"revision": 1},
         session_id="s-1")
    _log(kb, pool, "get_skill", name="deploy-checklist", trace={"revision": 1})
    _log(kb, pool, "get_skill", name="review-infra", trace={"revision": 1})
    _log(kb, pool, "get_skill_file", name="deploy-checklist", args={"path": "a.md"})
    _log(kb, pool, "search_skills", query="deploy", result_count=2,
         trace={"skills": ["deploy-checklist", "review-infra"]})
    _log(kb, pool, "search_skills", query="review", result_count=1,
         trace={"skills": ["review-infra"]})
    u = kb.skill_usage_for("deploy-checklist", days=30)
    assert (u["loads"], u["file_reads"], u["surfaced"]) == (2, 1, 1)
    assert u["name"] == "deploy-checklist" and u["days"] == 30 and u["tz"] == "UTC"
    assert u["last_loaded_at"] is not None and u["surfaced_since"] is not None
    assert [r["revision"] for r in u["recent_loads"]] == [1, 1]
    assert u["recent_loads"][-1]["session_id"] == "s-1"
    assert u["recent_loads"][0]["turn_id"] is not None


def test_usage_excludes_eval_traffic_errors_and_rows_outside_the_window(kb, pool):
    _skill(kb, "deploy-checklist")
    _log(kb, pool, "get_skill", name="deploy-checklist")
    _log(kb, pool, "get_skill", name="deploy-checklist", caller="eval")
    _log(kb, pool, "get_skill", name="deploy-checklist",
         error="revision_not_found: skill 'deploy-checklist' has no revision 9")
    _log(kb, pool, "get_skill", name="deploy-checklist", ago=timedelta(days=40))
    assert kb.skill_usage_for("deploy-checklist", days=30)["loads"] == 1
    assert kb.skill_usage_for("deploy-checklist", days=90)["loads"] == 2


def test_recent_loads_prefer_the_served_revision_then_the_requested_one(kb, pool):
    _skill(kb, "deploy-checklist")
    _log(kb, pool, "get_skill", name="deploy-checklist", args={"revision": None})
    _log(kb, pool, "get_skill", name="deploy-checklist", args={"revision": 2})
    _log(kb, pool, "get_skill", name="deploy-checklist", args={"revision": None},
         trace={"revision": 3})
    _log(kb, pool, "get_skill", name="deploy-checklist", trace={"revision": "abc"})
    revs = [r["revision"] for r in kb.skill_usage_for("deploy-checklist", days=7)["recent_loads"]]
    assert revs == [None, 3, 2, None]     # newest first; malformed never fails the query


def test_library_usage_per_day_is_zero_filled_and_counts_misses(kb, pool):
    _log(kb, pool, "search_skills", query="deploy", result_count=0, trace={"skills": []})
    _log(kb, pool, "search_skills", query=None, result_count=0, trace={"skills": []})
    _log(kb, pool, "search_skills", query="review", result_count=1,
         trace={"skills": ["review-infra"]})
    u = kb.skill_usage(days=7)
    days = u["searches_per_day"]
    assert len(days) == 7
    assert [d["day"] for d in days] == sorted(d["day"] for d in days)
    assert (days[-1]["searches"], days[-1]["missed"]) == (3, 1)
    assert all(d["searches"] == 0 and d["missed"] == 0 for d in days[:-1])
    assert u["days"] == 7 and u["tz"] == "UTC" and u["since"] is not None


def test_missed_searches_group_variants_and_skip_paged_and_blank_queries(kb, pool):
    for q in ("Deploy  Checklist", "deploy checklist", " deploy checklist "):
        _log(kb, pool, "search_skills", query=q, result_count=0, trace={"skills": []})
    # A later page past the end of a matching query is not a miss.
    _log(kb, pool, "search_skills", query="review", result_count=0,
         args={"cursor": "NTA="}, trace={"skills": []})
    _log(kb, pool, "search_skills", query="   ", result_count=0, trace={"skills": []})
    u = kb.skill_usage(days=30)
    assert [(m["query"], m["count"]) for m in u["missed_searches"]] == [
        ("deploy checklist", 3)]
    assert u["missed_searches"][0]["last_at"] is not None
    assert sum(d["missed"] for d in u["searches_per_day"]) == 3


def test_missing_requests_count_not_found_errors_only(kb, pool):
    _skill(kb, "deploy-checklist")
    for _ in range(2):
        _log(kb, pool, "get_skill", name="release-notes",
             error="not_found: skill 'release-notes' does not exist")
    _log(kb, pool, "get_skill_file", name="release-notes",
         error="not_found: skill 'release-notes' does not exist")
    _log(kb, pool, "get_skill_file", name="deploy-checklist",
         error="file_not_found: skill 'deploy-checklist' revision 1 has no file 'x.md'")
    _log(kb, pool, "get_skill", name="deploy-checklist",
         error="revision_not_found: skill 'deploy-checklist' has no revision 9")
    u = kb.skill_usage(days=30)
    assert [(m["name"], m["count"]) for m in u["missing_requests"]] == [("release-notes", 3)]


def test_never_loaded_lists_active_unloaded_skills_only(kb, pool):
    for n in ("alpha-skill", "beta-skill", "gamma-skill"):
        _skill(kb, n)
    kb.delete_skill("gamma-skill", 1)
    _log(kb, pool, "get_skill", name="alpha-skill", trace={"revision": 1})
    u = kb.skill_usage(days=30)
    assert u["never_loaded"] == ["beta-skill"]
    assert set(u["per_skill"]) == {"alpha-skill"}
    assert u["per_skill"]["alpha-skill"]["loads"] == 1


def test_surfaced_since_is_null_until_a_search_records_names(kb, pool):
    _skill(kb, "alpha-skill")
    _log(kb, pool, "search_skills", query="alpha", result_count=1)   # row with no trace
    assert kb.skill_usage(days=30)["surfaced_since"] is None
    assert kb.skill_usage_for("alpha-skill", days=30)["surfaced_since"] is None
    _log(kb, pool, "search_skills", query="alpha", result_count=1,
         trace={"skills": ["alpha-skill"]})
    assert kb.skill_usage(days=30)["surfaced_since"] is not None


def test_usage_accepts_a_named_timezone(kb, pool):
    u = kb.skill_usage(days=7, tz="Asia/Tokyo")
    assert u["tz"] == "Asia/Tokyo" and len(u["searches_per_day"]) == 7


@pytest.mark.parametrize("kw", [{"days": 14}, {"days": True}, {"days": "30"},
                                {"tz": "Nowhere/Land"}, {"tz": ""}])
def test_usage_rejects_bad_days_and_timezones(kb, kw):
    with pytest.raises(InvalidArgument):
        kb.skill_usage(**kw)
    with pytest.raises(InvalidArgument):
        kb.skill_usage_for("alpha-skill", **kw)


def test_usage_for_rejects_a_malformed_name(kb):
    with pytest.raises(InvalidPackage):
        kb.skill_usage_for("Bad Name", days=30)
