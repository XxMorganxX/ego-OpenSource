"""Library archive: approved revisions only, deterministic bytes, a content ETag."""
import io
import json
import tarfile

import pytest

from kb_core import library_export as LX


def _md(name, body="v1\n"):
    return f"---\nname: {name}\ndescription: Example for tests.\n---\n\n{body}"


def _agent(name, body="Audit.\n"):
    return f"---\nname: {name}\ndescription: Audits.\ntools: Read\n---\n\n{body}"


def _members(body):
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
        return {m.name: (m, tf.extractfile(m).read()) for m in tf.getmembers()}


def _approve(kb, name, rev, kind="skill"):
    get = kb.get_skill if kind == "skill" else kb.get_agent
    review = kb.review_skill if kind == "skill" else kb.review_agent
    review(name, rev, get(name, rev)["package_hash"], True, "read")


def test_build_archive_is_deterministic_and_laid_out_by_kind():
    items = [{"name": "review-infra", "revision": 2, "package_hash": "a" * 64,
              "description": "d", "files": {"SKILL.md": _md("review-infra"), "ref/x.md": "x\n"}}]
    e1, b1 = LX.build_archive("skill", items)
    e2, b2 = LX.build_archive("skill", list(reversed(items)))
    assert (e1, b1) == (e2, b2)
    m = _members(b1)
    assert list(m) == ["manifest.json", "skills/review-infra/SKILL.md", "skills/review-infra/ref/x.md"]
    for info, _ in m.values():
        assert info.isfile() and info.mtime == 0 and info.mode == 0o644 and info.uid == 0
    manifest = json.loads(m["manifest.json"][1])
    assert manifest["format"] == 1 and manifest["kind"] == "skill"
    assert manifest["items"][0]["files"][0]["path"] == "SKILL.md"
    _, ab = LX.build_archive("agent", [{"name": "code-auditor", "revision": 1, "package_hash": "b" * 64,
                                        "description": "d", "files": {"AGENT.md": _agent("code-auditor")}}])
    assert list(_members(ab)) == ["manifest.json", "agents/code-auditor.md"]


def test_etag_moves_with_any_served_change_and_only_then():
    base = [{"name": "a", "revision": 1, "package_hash": "1" * 64, "description": "d", "files": {}}]
    e = LX.etag_for("skill", base)
    assert e == LX.etag_for("skill", [dict(base[0], description="other text")])
    assert e != LX.etag_for("skill", [dict(base[0], revision=2)])
    assert e != LX.etag_for("agent", base)
    assert e != LX.etag_for("skill", [])


def test_export_serves_only_approved_active_items(kb):
    kb.upsert_skill("deploy-checklist", None, {"SKILL.md": _md("deploy-checklist")}, "c")
    _approve(kb, "deploy-checklist", 1)
    kb.upsert_skill("deploy-checklist", None, {"SKILL.md": _md("deploy-checklist", "PENDING TEXT\n")}, "e")
    kb.upsert_skill("never-reviewed", None, {"SKILL.md": _md("never-reviewed")}, "c")
    kb.upsert_skill("gone-skill", None, {"SKILL.md": _md("gone-skill")}, "c")
    _approve(kb, "gone-skill", 1)
    kb.delete_skill("gone-skill")
    kb.upsert_agent("code-auditor", None, _agent("code-auditor"), "c")
    _approve(kb, "code-auditor", 1, kind="agent")
    etag, body = kb.export_library("skill")
    m = _members(body)
    assert list(m) == ["manifest.json", "skills/deploy-checklist/SKILL.md"]
    assert b"PENDING TEXT" not in m["skills/deploy-checklist/SKILL.md"][1]
    assert etag == kb.library_etag("skill")
    _, abody = kb.export_library("agent")
    assert list(_members(abody)) == ["manifest.json", "agents/code-auditor.md"]
    with pytest.raises(ValueError):
        LX.build_archive("tool", [])
