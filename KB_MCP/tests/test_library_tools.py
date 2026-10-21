"""The library on the MCP surface: approved-only reads, overwrite writes, agents, hash-bound
review, the pending list, and the separate public library-write gate."""
import asyncio
import io
import tarfile

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from kb_mcp import server as S
from kb_mcp.server import SERVER_INSTRUCTIONS, build_server

NEW = {"review_skill", "search_agents", "get_agent", "upsert_agent", "delete_agent",
       "review_agent", "list_pending_reviews"}


def _md(name, body="v1\n"):
    return f"---\nname: {name}\ndescription: Example for tests.\n---\n\n{body}"


def _call(server, tool, args):
    return asyncio.run(server.call_tool(tool, args))[1]


def _exported(kb, kind):
    with tarfile.open(fileobj=io.BytesIO(kb.export_library(kind)[1]), mode="r:gz") as tf:
        return sorted(m.name for m in tf)


def test_new_tools_are_registered_and_classified(kb):
    server = build_server(kb)
    assert NEW <= {t.name for t in asyncio.run(server.list_tools())}
    kinds = {r["tool"]: r["kind"] for r in kb.tool_states()}
    for w in ("review_skill", "upsert_agent", "delete_agent", "review_agent"):
        assert kinds[w] == "write", w
    for r in ("search_agents", "get_agent", "list_pending_reviews"):
        assert kinds[r] == "read", r


def test_expected_revision_is_optional_on_every_write(kb):
    tools = {t.name: t for t in build_server(kb)._tool_manager.list_tools()}
    assert set(tools["upsert_skill"].parameters["required"]) == {"name", "files", "change_summary"}
    assert set(tools["upsert_agent"].parameters["required"]) == {"name", "agent_md", "change_summary"}
    assert tools["delete_skill"].parameters["required"] == ["name"]
    assert set(tools["review_skill"].parameters["required"]) == {
        "name", "revision", "package_hash", "approve", "notes"}


def test_write_review_then_read_serves_only_approved(kb):
    server = build_server(kb)
    out = _call(server, "upsert_skill", {"name": "review-infra", "files": {"SKILL.md": _md("review-infra")},
                                         "change_summary": "create", "agent_label": "claude-code"})
    assert out["review_status"] == "pending" and "review_skill" in out["next_step"]
    with pytest.raises(ToolError, match="not_approved: .*revision 1 is pending review"):
        _call(server, "get_skill", {"name": "review-infra"})
    assert _call(server, "search_skills", {})["skills"] == []
    assert [p["name"] for p in _call(server, "list_pending_reviews", {})["pending"]] == ["review-infra"]
    pending = _call(server, "get_skill", {"name": "review-infra", "revision": 1})
    with pytest.raises(ToolError, match="review_hash_mismatch: "):
        _call(server, "review_skill", {"name": "review-infra", "revision": 1, "package_hash": "f" * 64,
                                       "approve": True, "notes": "read"})
    done = _call(server, "review_skill", {"name": "review-infra", "revision": 1,
                                          "package_hash": pending["package_hash"], "approve": True,
                                          "notes": "read SKILL.md in full", "agent_label": "claude-code"})
    assert done["approved_revision"] == 1
    _call(server, "upsert_skill", {"name": "review-infra", "files": {"SKILL.md": _md("review-infra", "v2\n")},
                                   "change_summary": "overwrite"})
    got = _call(server, "get_skill", {"name": "review-infra"})
    assert got["revision"] == 1 and got["pending_revision"] == 2
    assert _call(server, "search_skills", {})["skills"][0]["pending_revision"] == 2


def test_agents_round_trip(kb):
    server = build_server(kb)
    agent = _md("code-auditor", "You audit.\n").replace("description:", "tools: Read\ndescription:")
    out = _call(server, "upsert_agent", {"name": "code-auditor", "agent_md": agent, "change_summary": "c"})
    got = _call(server, "get_agent", {"name": "code-auditor", "revision": 1})
    assert got["agent_md"] == agent and got["kind"] == "agent"
    _call(server, "review_agent", {"name": "code-auditor", "revision": 1,
                                   "package_hash": out["package_hash"], "approve": True, "notes": "read"})
    assert [a["name"] for a in _call(server, "search_agents", {})["agents"]] == ["code-auditor"]
    assert _call(server, "delete_agent", {"name": "code-auditor"})["archived"] is True
    assert _call(server, "search_agents", {})["agents"] == []


def test_a_fix_to_an_archived_agent_serves_nothing_until_approved(kb):
    """Reactivation happens on approval, not on the write."""
    server = build_server(kb)
    v1 = _md("beta-agent", "v1\n")
    out = _call(server, "upsert_agent", {"name": "beta-agent", "agent_md": v1, "change_summary": "c"})
    _call(server, "review_agent", {"name": "beta-agent", "revision": 1,
                                   "package_hash": out["package_hash"], "approve": True, "notes": "read"})
    _call(server, "delete_agent", {"name": "beta-agent"})
    fix = _call(server, "upsert_agent", {"name": "beta-agent", "agent_md": _md("beta-agent", "FIX\n"),
                                         "change_summary": "fix"})
    assert fix["archived"] is True and fix["reactivated"] is False
    assert "approving it restores" in fix["next_step"]
    assert _call(server, "search_agents", {})["agents"] == []
    assert _call(server, "get_agent", {"name": "beta-agent"})["archived"] is True
    assert _exported(kb, "agent") == ["manifest.json"]                # devices get nothing
    assert [p["name"] for p in _call(server, "list_pending_reviews", {})["pending"]] == ["beta-agent"]
    done = _call(server, "review_agent", {"name": "beta-agent", "revision": 2,
                                          "package_hash": fix["package_hash"], "approve": True,
                                          "notes": "read in full"})
    assert done["reactivated"] is True
    got = _call(server, "get_agent", {"name": "beta-agent"})
    assert got["archived"] is False and got["revision"] == 2 and got["agent_md"].endswith("FIX\n")
    assert [a["name"] for a in _call(server, "search_agents", {})["agents"]] == ["beta-agent"]
    assert _exported(kb, "agent") == ["agents/beta-agent.md", "manifest.json"]


def test_agent_text_is_served_unframed(kb):
    server = build_server(kb)
    md = _md("code-auditor", "You audit.\n")
    _call(server, "upsert_agent", {"name": "code-auditor", "agent_md": md, "change_summary": "c"})
    got = _call(server, "get_agent", {"name": "code-auditor", "revision": 1})
    assert got["agent_md"] == md and "<untrusted-" not in str(got)


def _public(kb, **kw):
    return build_server(kb, host="127.0.0.1", port=3199, public_hostname="kb-mcp.example.com",
                        authenticated=True, **kw)


def test_library_writes_on_the_public_listener_need_their_own_flag(kb):
    off = _public(kb, public_write_tools="all")
    names = {t.name for t in asyncio.run(off.list_tools())}
    assert not names & S.LIBRARY_WRITE_TOOLS
    assert {"get_skill", "get_agent", "list_pending_reviews"} <= names
    with pytest.raises(Exception, match="KB_MCP_PUBLIC_LIBRARY_WRITES"):
        _call(off, "upsert_agent", {"name": "x", "agent_md": "x", "change_summary": "x"})
    on = _public(kb, public_library_writes=True)
    assert S.LIBRARY_WRITE_TOOLS <= {t.name for t in asyncio.run(on.list_tools())}
    with pytest.raises(ValueError, match="KB_MCP_PUBLIC_LIBRARY_WRITES"):
        _public(kb, public_write_tools=frozenset({"upsert_skill"}))


def test_public_library_writes_env_parsing():
    assert S.public_library_writes_from_env({"KB_MCP_PUBLIC_LIBRARY_WRITES": "1"}) is True
    assert S.public_library_writes_from_env({"KB_MCP_PUBLIC_LIBRARY_WRITES": "on"}) is True
    assert S.public_library_writes_from_env({}) is False
    assert S.public_library_writes_from_env({"KB_MCP_PUBLIC_LIBRARY_WRITES": "all"}) is False


def test_server_instructions_require_review_after_every_write():
    text = SERVER_INSTRUCTIONS
    assert "review_skill" in text and "review_agent" in text and "EVERY" in text
    assert "revision_conflict, reread" not in text


def test_every_agent_facing_read_asks_for_approved(kb, monkeypatch):
    """The KnowledgeBase defaults are the dashboard's; MCP must opt into approved."""
    seen = []
    for meth in ("get_skill", "get_agent"):
        orig = getattr(kb, meth)
        monkeypatch.setattr(kb, meth, lambda *a, _o=orig, **k: (seen.append(k.get("default")), _o(*a, **k))[1])
    for meth in ("search_skills", "search_agents"):
        orig = getattr(kb, meth)
        monkeypatch.setattr(kb, meth, lambda *a, _o=orig, **k: (seen.append(k.get("include_unapproved")), _o(*a, **k))[1])
    server = build_server(kb)
    for tool, args in (("get_skill", {"name": "x"}), ("get_agent", {"name": "x"}),
                       ("search_skills", {}), ("search_agents", {})):
        try:
            _call(server, tool, args)
        except ToolError:
            pass
    assert seen == ["approved", "approved", False, False]
