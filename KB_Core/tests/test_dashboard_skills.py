"""Dashboard Skills page routes: read-only wrappers over the skill registry + usage."""
import pytest
from fastapi.testclient import TestClient

from dashboard.api.app import create_app


def _md(name, desc="Example skill for tests.", body="Body\n"):
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


@pytest.fixture
def client(kb):
    return TestClient(create_app(kb))


@pytest.fixture
def deploy(kb):
    """deploy-checklist at revision 2, with a nested supporting file whose name needs
    URL encoding (space and #)."""
    kb.upsert_skill("deploy-checklist", None,
                    {"SKILL.md": _md("deploy-checklist", "Steps before a deploy.")}, "create")
    kb.upsert_skill("deploy-checklist", 1,
                    {"SKILL.md": _md("deploy-checklist", "Steps before a deploy.", "v2\n"),
                     "references/deploy notes#1.md": "notes\n"}, "add notes")


def test_list_search_and_archived_toggle(kb, client, deploy):
    kb.upsert_skill("old-skill", None, {"SKILL.md": _md("old-skill")}, "create")
    kb.delete_skill("old-skill", 1)
    body = client.get("/api/skills").json()
    assert [s["name"] for s in body["skills"]] == ["deploy-checklist"]
    assert body["next_cursor"] is None and body["query"] is None
    assert isinstance(body["skills"][0]["updated_at"], str)
    archived = client.get("/api/skills", params={"archived": "true"}).json()
    assert [s["name"] for s in archived["skills"]] == ["deploy-checklist", "old-skill"]
    found = client.get("/api/skills", params={"q": "deploy"}).json()
    assert [s["name"] for s in found["skills"]] == ["deploy-checklist"]
    assert found["query"] == "deploy"


def test_detail_at_current_and_pinned_revision(client, deploy):
    d = client.get("/api/skills/deploy-checklist").json()
    assert d["revision"] == 2 == d["current_revision"]
    assert [f["path"] for f in d["files"]] == ["SKILL.md", "references/deploy notes#1.md"]
    assert [h["revision"] for h in d["history"]] == [2, 1]
    assert isinstance(d["history"][0]["created_at"], str) and d["skill_md"].endswith("v2\n")
    old = client.get("/api/skills/deploy-checklist", params={"revision": 1}).json()
    assert (old["revision"], old["current_revision"]) == (1, 2)


def test_file_route_requires_revision_and_reads_nested_paths(client, deploy):
    url = "/api/skills/deploy-checklist/files/references/deploy%20notes%231.md"
    r = client.get(url, params={"revision": 2})
    assert r.status_code == 200
    assert r.json()["content"] == "notes\n" and r.json()["path"] == "references/deploy notes#1.md"
    assert client.get(url).status_code == 422                       # revision is required
    gone = client.get(url, params={"revision": 1})                  # not in revision 1
    assert gone.status_code == 404 and gone.json()["detail"].startswith("file_not_found:")


@pytest.mark.parametrize("url,status,code", [
    ("/api/skills/no-such-skill", 404, "not_found:"),
    ("/api/skills/deploy-checklist?revision=9", 404, "revision_not_found:"),
    ("/api/skills/Bad_Name", 422, "invalid_package:"),
    ("/api/skills/deploy-checklist?revision=0", 422, "invalid_argument:"),
    ("/api/skills/deploy-checklist/files/missing.md?revision=1", 404, "file_not_found:"),
    ("/api/skills/no-such-skill/files/SKILL.md?revision=1", 404, "not_found:"),
])
def test_registry_errors_map_to_http_status_with_the_stable_code(client, deploy,
                                                                 url, status, code):
    r = client.get(url)
    assert r.status_code == status and r.json()["detail"].startswith(code)


def test_a_skill_named_usage_does_not_collide_with_library_usage(kb, client):
    kb.upsert_skill("usage", None, {"SKILL.md": _md("usage")}, "create")
    assert client.get("/api/skills/usage").json()["name"] == "usage"
    assert client.get("/api/skills/usage/usage").json()["name"] == "usage"
    lib = client.get("/api/skill-usage").json()
    assert lib["days"] == 30 and lib["never_loaded"] == ["usage"]


def test_usage_routes_validate_days_and_tz(kb, client, deploy):
    kb.log_trace(tool="get_skill", args={"name": "deploy-checklist"}, latency_ms=1)
    one = client.get("/api/skills/deploy-checklist/usage", params={"days": 7}).json()
    assert one["loads"] == 1 and isinstance(one["since"], str)
    assert client.get("/api/skill-usage", params={"days": 14}).status_code == 422
    assert client.get("/api/skill-usage", params={"tz": "Nowhere/Land"}).status_code == 422
    assert client.get("/api/skills/deploy-checklist/usage",
                      params={"days": "abc"}).status_code == 422
    lib = client.get("/api/skill-usage", params={"days": 7, "tz": "Asia/Tokyo"}).json()
    assert len(lib["searches_per_day"]) == 7 and lib["tz"] == "Asia/Tokyo"
    assert isinstance(lib["searches_per_day"][0]["day"], str)
    assert lib["per_skill"]["deploy-checklist"]["loads"] == 1


@pytest.mark.parametrize("path", ["/api/skills", "/api/skills/deploy-checklist",
                                  "/api/skill-usage",
                                  "/api/skills/deploy-checklist/files/SKILL.md"])
def test_skills_routes_have_no_write_methods(client, path):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(path).status_code == 405
