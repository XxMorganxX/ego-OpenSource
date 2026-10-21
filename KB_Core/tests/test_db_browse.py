"""The raw Postgres table browser (`/api/db/*`) and its password gate."""

import pytest
from fastapi.testclient import TestClient

from dashboard.api.app import create_app
from kb_core.domain import NodeRecord

PASSWORD = "s3cret"
AUTH = {"X-DB-Password": PASSWORD}


@pytest.fixture
def gated(kb):
    """A client whose db-browser endpoints are gated by a configured password."""
    return TestClient(create_app(kb, db_viewer_password=PASSWORD))


def _seed(kb):
    kb.upsert(NodeRecord(source="agent", external_id="a", type="note",
                         content="alpha body", slug="alpha", parent_path="projects",
                         title="Alpha", tags=["x"]))


def test_tables_lists_real_postgres_tables(gated, kb):
    _seed(kb)
    body = gated.get("/api/db/tables", headers=AUTH).json()
    by_name = {t["name"]: t for t in body["tables"]}
    # The engine's own tables show up 1:1 — nothing curated away.
    assert "nodes" in by_name
    assert "schema_migrations" in by_name
    nodes = by_name["nodes"]
    assert nodes["schema"] == "public"
    assert nodes["columns"] > 0
    # System catalogs are not "our" tables.
    assert not any(t["schema"] in ("pg_catalog", "information_schema") for t in body["tables"])


def test_table_page_returns_columns_pk_rows_and_total(gated, kb):
    _seed(kb)
    body = gated.get("/api/db/table/public/nodes", headers=AUTH).json()
    assert body["schema"] == "public" and body["name"] == "nodes"
    assert body["primary_key"] == ["id"]
    col_names = {c["name"] for c in body["columns"]}
    assert {"id", "path", "title"} <= col_names
    # The note plus the "projects" folder auto-created for it.
    assert body["total"] == 2
    alpha = next(r for r in body["rows"] if r["title"] == "Alpha")
    assert alpha["path"] == "projects.alpha"
    # Non-JSON-native cell values are coerced to strings (id is a UUID).
    assert isinstance(alpha["id"], str)


def test_pagination_caps_limit_and_reports_offset(gated, kb):
    for i in range(3):
        kb.upsert(NodeRecord(source="agent", external_id=f"n{i}", type="note",
                             content="c", slug=f"n{i}", parent_path="notes"))
    body = gated.get("/api/db/table/public/nodes",
                     params={"limit": 10_000, "offset": 1}, headers=AUTH).json()
    assert body["limit"] == 500          # clamped to ROW_LIMIT_MAX
    assert body["offset"] == 1
    assert body["total"] >= 3


def test_unknown_table_is_404(gated):
    assert gated.get("/api/db/table/public/no_such_table", headers=AUTH).status_code == 404


def test_missing_or_wrong_password_is_401(gated):
    assert gated.get("/api/db/tables").status_code == 401
    assert gated.get("/api/db/tables", headers={"X-DB-Password": "nope"}).status_code == 401
    assert gated.post("/api/db/auth", headers=AUTH).json() == {"ok": True}


def test_unconfigured_password_locks_endpoints_503(kb):
    # No db_viewer_password -> the browser is fully locked, even with a header.
    client = TestClient(create_app(kb))
    assert client.get("/api/db/tables").status_code == 503
    assert client.get("/api/db/tables", headers=AUTH).status_code == 503
