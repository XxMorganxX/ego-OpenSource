import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient

from dashboard.api.app import create_app
from kb_core.domain import NodeRecord
from kb_core.identity import node_id


@pytest.fixture
def client(kb):
    return TestClient(create_app(kb))


def _seed(kb):
    kb.upsert(NodeRecord(source="agent", external_id="a", type="note",
                         content="alpha body", slug="alpha", parent_path="projects",
                         title="Alpha", tags=["x"]))
    kb.upsert(NodeRecord(source="agent", external_id="b", type="note",
                         content="beta body", slug="beta", parent_path="projects"))


def test_tree_returns_all_node_metadata_without_content(client, kb):
    _seed(kb)
    resp = client.get("/api/tree")
    assert resp.status_code == 200
    rows = resp.json()
    paths = {r["path"] for r in rows}
    # the two notes plus the auto-created "projects" folder
    assert {"projects", "projects.alpha", "projects.beta"} <= paths
    alpha = next(r for r in rows if r["path"] == "projects.alpha")
    assert alpha["type"] == "note"
    assert alpha["title"] == "Alpha"
    assert alpha["tags"] == ["x"]
    assert "importance" in alpha  # significance score travels with node metadata
    assert alpha["importance_modifiers"] == {}  # and its per-modifier contribution breakdown
    assert "content" not in alpha  # tree is metadata-only


def test_node_detail_exposes_importance_and_contributions(client, kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="imp", type="note",
                                content="boosted body", slug="boosted", parent_path="notes"))
    kb.set_importance(node.id, 1.0)  # manual modifier of 1.0 -> importance 2.0
    body = client.get(f"/api/node/{node.id}").json()
    assert body["importance"] == 2.0                      # the aggregate value
    assert body["importance_modifiers"]["manual"] == 1.0  # the contribution that produced it


def test_processing_lists_memories_being_enriched(client, kb):
    n = kb.upsert(NodeRecord(source="agent", external_id="proc", type="note",
                             content="c", slug="proc", parent_path="notes", title="Proc"))
    # Nothing processing yet.
    assert client.get("/api/ingest/processing").json()["nodes"] == []
    # Flag stale then claim it -> enters 'processing'.
    kb.flag_stale_enrichment()
    kb.claim_enrichment()
    body = client.get("/api/ingest/processing").json()
    assert [x["id"] for x in body["nodes"]] == [str(n.id)]
    assert body["nodes"][0]["title"] == "Proc"


def test_excluded_memories_endpoint_lists_noise_and_dead_labeled(client, kb):
    noise = kb.upsert(NodeRecord(source="agent", external_id="n", type="note",
                                 content="zzzz", slug="n", parent_path="notes", title="Noise"))
    dead = kb.upsert(NodeRecord(source="agent", external_id="d", type="note",
                                content="old event", slug="d", parent_path="notes", title="Dead"))
    kb.upsert(NodeRecord(source="agent", external_id="ok", type="note",
                         content="real", slug="ok", parent_path="notes", title="Ok"))
    kb.set_entity_eligible(noise.id, False, reason="random characters")
    kb.mark_dead(dead.id, reason="event has passed")

    rows = client.get("/api/memories/excluded").json()["memories"]
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {str(noise.id), str(dead.id)}          # actives excluded
    assert by_id[str(noise.id)]["lifecycle"] == "noise"
    assert by_id[str(noise.id)]["reason"] == "random characters"
    assert by_id[str(dead.id)]["lifecycle"] == "dead"
    assert by_id[str(dead.id)]["reason"] == "event has passed"

    dead_only = client.get("/api/memories/excluded?kind=dead").json()["memories"]
    assert [r["id"] for r in dead_only] == [str(dead.id)]


def test_retire_then_revive_endpoints(client, kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="r", type="note",
                                content="c", slug="r", parent_path="notes", title="R"))
    retired = client.post(f"/api/node/{node.id}/retire", json={"reason": "stale"}).json()
    assert retired["lifecycle"] == "dead" and retired["dead_reason"] == "stale"
    revived = client.post(f"/api/node/{node.id}/revive").json()
    assert revived["lifecycle"] == "active" and revived["dead_at"] is None


def test_node_detail_exposes_external_id(client, kb):
    # The original source identifier (a file path for obsidian) is surfaced so a renamed
    # node still shows where it came from.
    node = kb.upsert(NodeRecord(source="obsidian", external_id="Vandelay/Corp/Atlas.md",
                                type="note", content="c", slug="atlas", parent_path="notes"))
    body = client.get(f"/api/node/{node.id}").json()
    assert body["external_id"] == "Vandelay/Corp/Atlas.md"


def test_node_detail_exposes_project_dormant_days(client, kb):
    node = kb.upsert(NodeRecord(source="obsidian", external_id="pd", type="note",
                                content="a spec", slug="spec",
                                parent_path="Obsidian.Projects.repo.docs"))
    body = client.get(f"/api/node/{node.id}").json()
    assert body["project_dormant_days"] == 0            # ingested just now
    plain = kb.upsert(NodeRecord(source="agent", external_id="np", type="note",
                                 content="c", slug="np", parent_path="notes"))
    assert client.get(f"/api/node/{plain.id}").json()["project_dormant_days"] is None


def test_tree_excludes_entities(client, kb):
    _seed(kb)
    kb.upsert_entity_class("people")
    kb.upsert_entity("Avery", categories=["people"])
    paths = {r["path"] for r in client.get("/api/tree").json()}
    # memories are present; nothing from the reserved `entities` subtree leaks in
    assert "projects.alpha" in paths
    assert not any(p == "entities" or p.startswith("entities.") for p in paths)


def test_entities_endpoint_returns_classes_instances_and_containment(client, kb):
    kb.upsert_entity_class("people")
    kb.upsert_entity("Avery", categories=["people"])
    data = client.get("/api/entities").json()
    by_role = {n["role"]: n for n in data["nodes"]}
    root, klass, inst = by_role["root"], by_role["class"], by_role["instance"]
    assert root["path"] == "entities"
    assert klass["entity_type"] == "class"
    assert inst["entity_type"] == "instance"   # Phase 2: discriminator, not the class name
    assert inst["path"] == "entities.instances.Avery"   # flat; category is a link
    # classes hang off the hub (tree); instances hang off their class via the is_a link
    assert {"source": root["id"], "target": klass["id"], "kind": "containment"} in data["links"]
    assert {"source": klass["id"], "target": inst["id"], "kind": "containment"} in data["links"]


def test_entities_endpoint_hides_blocked_classes(client, kb):
    kb.upsert_entity_class("people", active=True)
    kb.upsert_entity_class("junk", active=True)
    kb.block_class("junk")
    roles = client.get("/api/entities").json()["nodes"]
    names = {n["title"] for n in roles if n["role"] == "class"}
    assert names == {"people"}                              # the tombstone is hidden
    # include_blocked surfaces it again for the curation view
    names_all = {n["title"] for n in
                 client.get("/api/entities", params={"include_blocked": "true"}).json()["nodes"]
                 if n["role"] == "class"}
    assert "junk" in names_all


def test_classes_endpoint_lists_status_and_counts(client, kb):
    cls = kb.upsert_entity_class("people", active=True)
    mem = kb.upsert(NodeRecord(source="agent", external_id="cm", type="note",
                               content="x", slug="cm", parent_path="notes"))
    kb.link_entity(mem.id, cls.node.id, "belongs_to")
    rows = client.get("/api/classes").json()["classes"]
    people = next(r for r in rows if r["name"] == "people")
    assert people["status"] == "active" and people["members"] == 1


def test_classes_endpoint_reports_voters(client, kb):
    mem = kb.upsert(NodeRecord(source="agent", external_id="v1", type="note",
                               content="x", slug="v1", parent_path="notes"))
    kb.stage_entity_candidate(mem.id, "class", "people")
    kb.upsert_entity_class("people", active=True)
    rows = client.get("/api/classes").json()["classes"]
    assert next(r for r in rows if r["name"] == "people")["voters"] == 1


def test_class_voters_endpoint_lists_feeding_memories(client, kb):
    mem = kb.upsert(NodeRecord(source="agent", external_id="v2", type="note",
                               content="x", slug="v2", parent_path="notes"))
    kb.stage_entity_candidate(mem.id, "class", "people")
    kb.upsert_entity_class("people", active=True)
    voters = client.get("/api/classes/people/voters").json()["voters"]
    assert [v["path"] for v in voters] == ["notes.v2"]


def test_block_and_unblock_endpoints(client, kb):
    mem = kb.upsert(NodeRecord(source="agent", external_id="v3", type="note",
                               content="x", slug="v3", parent_path="notes"))
    kb.stage_entity_candidate(mem.id, "class", "junk")
    kb.upsert_entity_class("junk", active=True)
    kb.mark_enriched(mem.id)
    resp = client.post("/api/classes/junk/block")
    assert resp.status_code == 200 and resp.json()["status"] == "blocked"
    assert resp.json()["requeued"] == 1                        # its voter was re-queued
    assert kb.get(mem.id).enrichment_status == "pending"
    assert client.get("/api/classes").json()["classes"] == []
    resp = client.post("/api/classes/junk/unblock")
    assert resp.json()["status"] == "inactive"
    assert client.post("/api/classes/missing/block").status_code == 404


def test_merge_classes_endpoint(client, kb):
    kb.upsert_entity_class("ppl", active=True)
    resp = client.post("/api/classes/ppl/merge", json={"target": "people"})
    assert resp.status_code == 200 and resp.json()["name"] == "people"
    assert kb.get_entity_class("ppl").status == "blocked"


def test_reparent_instance_endpoint(client, kb):
    inst = kb.promote_entity("animals", "Fido")
    resp = client.post(f"/api/entity/{inst.node.id}/reparent",
                       json={"new_category": "pets"})
    assert resp.status_code == 200
    assert resp.json()["entity_type"] == "instance"
    assert resp.json()["classes"] == ["pets"]   # recategorized via class membership


def test_node_memories_lists_linked_memories(client, kb):
    ent = kb.upsert_entity("Avery", categories=["people"])
    mem = kb.upsert(NodeRecord(source="agent", external_id="m1", type="note",
                               content="met Avery today", slug="m1", parent_path="notes"))
    kb.link_entity(mem.id, ent.node.id, "mentions")
    body = client.get(f"/api/node/{ent.node.id}/memories").json()
    assert "notes.m1" in {m["path"] for m in body["memories"]}
    # a node with no entity_links (e.g. the memory itself) gets an empty list, not a 404
    assert client.get(f"/api/node/{mem.id}/memories").json()["memories"] == []


def test_subtree_returns_nodes_and_containment_links(client, kb):
    kb.upsert(NodeRecord(source="agent", external_id="c", type="note",
                         content="child body", slug="child",
                         parent_path="projects.kb", title="Child"))
    resp = client.get("/api/subtree", params={"path": "projects"})
    assert resp.status_code == 200
    data = resp.json()
    by_path = {n["path"]: n for n in data["nodes"]}
    assert {"projects", "projects.kb", "projects.kb.child"} <= set(by_path)
    ids = {n["id"] for n in data["nodes"]}
    # every link connects two nodes that are present, kind=containment
    for link in data["links"]:
        assert link["kind"] == "containment"
        assert link["source"] in ids and link["target"] in ids
    # the child is linked to its parent folder
    child_id, kb_id = by_path["projects.kb.child"]["id"], by_path["projects.kb"]["id"]
    assert {"source": kb_id, "target": child_id, "kind": "containment"} in data["links"]


def test_ingest_log_endpoint_returns_recent_events(client, kb):
    kb.upsert(NodeRecord(source="webhook", external_id="il1", type="note",
                         content="logged body", slug="logged", parent_path="notes"))
    resp = client.get("/api/ingest/log", params={"limit": 10})
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert events  # at least the node_created event
    top = events[0]
    assert top["event"] == "node_created"
    assert top["source"] == "webhook"
    assert top["path"] == "notes.logged"
    assert isinstance(top["at"], str)  # ISO timestamp, JSON-safe

    filtered = client.get("/api/ingest/log", params={"source": "agent"}).json()["events"]
    assert all(e["source"] == "agent" for e in filtered)


def test_ingest_status_reports_enrichment_counts_and_sources(client, kb):
    kb.upsert(NodeRecord(source="agent", external_id="s1", type="note",
                         content="x", slug="s1", parent_path="notes"))
    body = client.get("/api/ingest/status").json()
    assert "enrichment" in body and isinstance(body["enrichment"], dict)
    # a freshly upserted node is flagged 'none' until the pipeline queues it
    assert sum(body["enrichment"].values()) >= 1
    assert isinstance(body["sources"], list)


def test_get_node_returns_content_and_404_for_unknown(client, kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="d", type="note",
                                content="full body here", slug="doc",
                                parent_path="notes", title="Doc"))
    resp = client.get(f"/api/node/{node.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "full body here"
    assert body["source"] == "agent"
    missing = client.get("/api/node/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404


def test_patch_updates_only_given_fields(client, kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="e", type="note",
                                content="original body", slug="edit-me",
                                parent_path="notes", title="Before"))
    resp = client.patch(f"/api/node/{node.id}", json={"title": "After"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "After"
    assert body["content"] == "original body"  # unchanged

def test_patch_empty_content_is_400(client, kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="f", type="note",
                                content="body", slug="x", parent_path="notes"))
    resp = client.patch(f"/api/node/{node.id}", json={"content": "   "})
    assert resp.status_code == 400

def test_move_relocates_node(client, kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="g", type="note",
                                content="movable", slug="mover", parent_path="a"))
    resp = client.post(f"/api/node/{node.id}/move", json={"new_parent_path": "b.c"})
    assert resp.status_code == 200
    assert resp.json()["path"] == "b.c.mover"

def test_move_into_self_is_400(client, kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="h", type="folder",
                                content="dir", slug="dir", parent_path="root"))
    resp = client.post(f"/api/node/{node.id}/move", json={"new_parent_path": "root.dir"})
    assert resp.status_code == 400

def test_delete_recursive_removes_subtree(client, kb):
    kb.upsert(NodeRecord(source="agent", external_id="i", type="note",
                         content="leaf", slug="leaf", parent_path="branch.twig"))
    branch = kb.get_by_path("branch")
    resp = client.delete(f"/api/node/{branch.id}", params={"recursive": "true"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] >= 2  # branch + twig + leaf
    assert kb.get_by_path("branch.twig.leaf") is None

def test_create_node_from_title(client, kb):
    resp = client.post("/api/node", json={
        "content": "brand new note", "title": "Fresh Idea", "parent_path": "inbox"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "inbox.Fresh_Idea"  # title sanitized into slug
    assert body["source"] == "agent"
    assert kb.get_by_path("inbox.Fresh_Idea") is not None

def test_create_node_requires_slug_or_title(client, kb):
    resp = client.post("/api/node", json={"content": "no name"})
    assert resp.status_code == 400


def test_classes_endpoint_includes_description_fields(client, kb):
    kb.upsert_entity_class("people", active=True, description="humans we know")
    row = next(c for c in client.get("/api/classes").json()["classes"]
               if c["name"] == "people")
    assert row["description"] == "humans we know"
    assert row["description_locked"] is False


def test_get_single_class(client, kb):
    kb.upsert_entity_class("people", active=True, description="humans")
    body = client.get("/api/classes/people").json()
    assert body["name"] == "people"
    assert body["description"] == "humans"
    assert client.get("/api/classes/ghosts").status_code == 404


def test_patch_class_sets_and_locks_description(client, kb):
    kb.upsert_entity_class("people", active=True, description="auto")
    body = client.patch("/api/classes/people", json={"description": "curated"}).json()
    assert body["description"] == "curated"
    assert body["description_locked"] is True
    assert client.patch("/api/classes/ghosts", json={"description": "x"}).status_code == 404


def test_curation_commands_lists_actions(client, kb):
    actions = {c["action"] for c in client.get("/api/curation/commands").json()["commands"]}
    assert {"merge", "prune", "rename", "create", "unblock", "pin", "unpin"} <= actions


def test_curation_create_preview_then_apply(client, kb):
    preview = client.post("/api/curation/create/preview", json={"name": "hobbies"}).json()
    assert preview["applied"] is False and preview["action"] == "create"
    result = client.post("/api/curation/create", json={"name": "hobbies"}).json()
    assert result["applied"] is True
    cls = client.get("/api/classes/hobbies").json()
    assert cls["pinned"] is True


def test_curation_merge_into_blocked_target_returns_409(client, kb):
    kb.upsert_entity_class("frameworks", active=True)
    kb.upsert_entity_class("tools", active=True)
    kb.block_class("tools")
    resp = client.post("/api/curation/merge", json={"source": "frameworks", "target": "tools"})
    assert resp.status_code == 409


def test_curation_prune_missing_class_returns_404(client, kb):
    resp = client.post("/api/curation/prune/preview", json={"name": "ghost"})
    assert resp.status_code == 404


def test_classes_endpoint_reports_pinned(client, kb):
    kb.upsert_entity_class("people", active=True)
    kb.set_class_pinned("people", True)
    cls = client.get("/api/classes/people").json()
    assert cls["pinned"] is True


def test_reanalyze_requests_a_recrawl_for_a_memory(client, kb):
    n = kb.upsert(NodeRecord(source="agent", external_id="rc", type="note",
                             content="c", slug="rc", parent_path="notes"))
    resp = client.post(f"/api/node/{n.id}/reanalyze")
    assert resp.status_code == 200
    assert resp.json() == {"requested": True, "node_id": str(n.id)}


def test_reanalyze_missing_node_returns_404(client):
    import uuid

    resp = client.post(f"/api/node/{uuid.uuid4()}/reanalyze")
    assert resp.status_code == 404


def test_reanalyze_rejects_non_memory_nodes(client, kb):
    # The auto-created parent folder isn't a crawlable memory -> rejected.
    kb.upsert(NodeRecord(source="agent", external_id="leaf", type="note",
                         content="c", slug="leaf", parent_path="recrawlfolder"))
    folder = kb.get_by_path("recrawlfolder")
    resp = client.post(f"/api/node/{folder.id}/reanalyze")
    assert resp.status_code == 400


def test_analysis_exclude_then_include_document(client, kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="x", type="note",
                                content="c", slug="doc", parent_path="projects"))
    excluded = client.post(f"/api/node/{node.id}/analysis-exclude", json={"retract": False}).json()
    assert excluded["analysis_excluded"] is True
    assert excluded["analysis_excluded_effective"] is True
    assert excluded["excluded_by_path"] is None          # own-flag, not inherited
    assert excluded["descendant_doc_count"] == 1          # the doc itself
    included = client.post(f"/api/node/{node.id}/analysis-include").json()
    assert included["analysis_excluded"] is False
    assert included["analysis_excluded_effective"] is False


def test_analysis_exclude_folder_reports_inherited_on_child(client, kb):
    kb.upsert(NodeRecord(source="agent", external_id="c", type="note",
                         content="c", slug="child", parent_path="scratch"))
    folder = kb.get_by_path("scratch")
    folder_view = client.post(f"/api/node/{folder.id}/analysis-exclude",
                              json={"retract": False}).json()
    assert folder_view["descendant_doc_count"] == 1       # one memory under the folder

    child = kb.get_by_path("scratch.child")
    child_view = client.get(f"/api/node/{child.id}").json()
    assert child_view["analysis_excluded"] is False       # own-flag unset
    assert child_view["analysis_excluded_effective"] is True
    assert child_view["excluded_by_path"] == "scratch"   # the ancestor causing it


def test_tree_marks_excluded_folder_and_its_descendants_effective(client, kb):
    # Excluding a folder dims its whole subtree in the explorer/graph: the folder and the doc
    # under it read effective, a sibling tree does not.
    kb.upsert(NodeRecord(source="agent", external_id="c", type="note",
                         content="c", slug="child", parent_path="scratch"))
    kb.upsert(NodeRecord(source="agent", external_id="k", type="note",
                         content="k", slug="keep", parent_path="projects"))
    kb.set_analysis_excluded(kb.get_by_path("scratch").id, True)

    by_path = {r["path"]: r for r in client.get("/api/tree").json()}
    assert by_path["scratch"]["analysis_excluded"] is True
    assert by_path["scratch"]["analysis_excluded_effective"] is True
    assert by_path["scratch.child"]["analysis_excluded"] is False        # own-flag unset
    assert by_path["scratch.child"]["analysis_excluded_effective"] is True  # inherited -> dimmed
    assert by_path["projects.keep"]["analysis_excluded_effective"] is False  # sibling tree untouched


def test_subtree_dims_descendants_of_an_ancestor_excluded_above_the_root(client, kb):
    # The excluded folder can sit ABOVE the opened subtree's root, so effective must resolve
    # against the full flagged set, not just the returned nodes.
    kb.upsert(NodeRecord(source="agent", external_id="g", type="note",
                         content="g", slug="leaf", parent_path="scratch.sub"))
    kb.set_analysis_excluded(kb.get_by_path("scratch").id, True)

    nodes = {r["path"]: r for r in client.get("/api/subtree", params={"path": "scratch.sub"}).json()["nodes"]}
    assert nodes["scratch.sub"]["analysis_excluded_effective"] is True       # ancestor above root
    assert nodes["scratch.sub.leaf"]["analysis_excluded_effective"] is True


def test_analysis_exclude_rejects_entity_nodes(client, kb):
    inst = kb.promote_entity("people", "Avery")
    resp = client.post(f"/api/node/{inst.node.id}/analysis-exclude", json={"retract": False})
    assert resp.status_code == 400


def test_set_significance_endpoint_cascades(client, kb):
    child = kb.upsert(NodeRecord(source="agent", external_id="c", type="note",
                                 content="child body", slug="child", parent_path="projects.kb"))
    folder = kb.get_by_path("projects.kb")
    resp = client.post(f"/api/node/{folder.id}/significance", json={"value": 1.5})
    assert resp.status_code == 200
    assert resp.json()["importance"] == 2.5                       # folder's own boost
    body = client.get(f"/api/node/{child.id}").json()
    assert body["importance"] == 2.5                              # inherited from the folder
    names = {f["name"]: f for f in body["factors"]}
    assert names["inherited"]["contribution"] == 1.5
    assert [s["path"] for s in body["inherited_from"]] == ["projects.kb"]


def _kinded_note(kb, ext, kind, content="standup sync notes"):
    from kb_core.domain import NodePatch
    n = kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                             content=content, slug=ext, parent_path="notes"))
    kb.update(n.id, NodePatch(doc_kind=kind))
    return n


def test_node_payloads_carry_doc_kind(client, kb):
    n = _kinded_note(kb, "dkd", "reference")
    assert client.get(f"/api/node/{n.id}").json()["doc_kind"] == "reference"
    row = next(r for r in client.get("/api/tree").json() if r["id"] == str(n.id))
    assert row["doc_kind"] == "reference"


def test_doc_kinds_rollup_gates_and_click_through(client, kb):
    ids = [_kinded_note(kb, f"dk{i}", "meeting notes").id for i in range(3)]
    _kinded_note(kb, "dkl", "implementation plan")

    kinds = client.get("/api/doc-kinds").json()["kinds"]
    assert kinds == [{"kind": "meeting notes", "count": 3, "established": True}]
    everything = client.get("/api/doc-kinds", params={"all": "true"}).json()["kinds"]
    assert {k["kind"] for k in everything} == {"meeting notes", "implementation plan"}

    mems = client.get("/api/doc-kinds/meeting%20notes/memories").json()["memories"]
    assert {m["id"] for m in mems} == {str(i) for i in ids}
    assert all(m["doc_kind"] == "meeting notes" for m in mems)

    # Retiring one of the three must drop the rollup count AND the click-through must
    # agree with it -- the rollup is live-only, so the listing behind it must be too.
    # (2 < DEFAULT_DOC_KIND_SUPPORT, so it also falls out of the established-only default
    # view -- use ?all=true to see the dropped count directly.)
    kb.mark_dead(ids[0], reason="stale")
    everything = client.get("/api/doc-kinds", params={"all": "true"}).json()["kinds"]
    kind_counts = {k["kind"]: k["count"] for k in everything}
    assert kind_counts["meeting notes"] == 2
    live_ids = {str(i) for i in ids[1:]}
    mems = client.get("/api/doc-kinds/meeting%20notes/memories").json()["memories"]
    assert {m["id"] for m in mems} == live_ids


def test_concept_members_carry_doc_kind(client, kb):
    n = _kinded_note(kb, "dkc", "meeting notes")
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": "c0", "prev_id": None, "parent_key": None, "level": 0,
         "name": "Acme Robotics", "kind": "area", "description": "club work",
         "members": [(str(n.id), "memory", 1.0)], "embedding": None}], [])
    concept = client.get("/api/concepts").json()["concepts"][0]
    assert concept["members"][0]["doc_kind"] == "meeting notes"


def test_node_detail_carries_significance_breakdown(client, kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="d", type="note",
                                content="b", slug="doc", parent_path="notes"))
    body = client.get(f"/api/node/{node.id}").json()
    assert "factors" in body and "inherited_from" in body
    assert body["effective_importance"] == 1.0                   # neutral, never decays
    assert body["descendant_count"] == 0


def test_merge_suggestions_list_and_reject(client, kb):
    a = kb.upsert_entity("Acme")
    b = kb.upsert_entity("Acme University")
    cid = kb.stage_merge_candidate(
        *sorted([a.node.id, b.node.id], key=str),
        "instance", 0.1, "same uni", b.node.id,
    )
    r = client.get("/api/merge-suggestions")
    assert r.status_code == 200 and any(s["id"] == cid for s in r.json())
    r = client.post(f"/api/merge-suggestions/{cid}/reject", json={"reason": "no"})
    assert r.status_code == 200
    assert client.get("/api/merge-suggestions").json() == []


def test_merge_suggestions_approve(client, kb):
    a = kb.upsert_entity("Stanford")
    b = kb.upsert_entity("Stanford University")
    target, source = sorted([a, b], key=lambda e: str(e.node.id))
    cid = kb.stage_merge_candidate(
        target.node.id, source.node.id,
        "instance", 0.05, "same uni", target.node.id,
    )
    r = client.post(f"/api/merge-suggestions/{cid}/approve", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["merged"] is True
    assert "target_id" in body
    # after approval the suggestion is resolved (no longer pending)
    assert client.get("/api/merge-suggestions").json() == []


def test_merge_suggestions_approve_missing_returns_404(client, kb):
    r = client.post("/api/merge-suggestions/9999999/approve", json={})
    assert r.status_code == 404


def test_merge_suggestions_reject_missing_returns_404(client, kb):
    r = client.post("/api/merge-suggestions/9999999/reject", json={})
    assert r.status_code == 404


def test_tree_exposes_updated_at(client, kb):
    _seed(kb)
    rows = client.get("/api/tree").json()
    alpha = next(r for r in rows if r["path"] == "projects.alpha")
    assert isinstance(alpha["updated_at"], str) and alpha["updated_at"]


def _content_changed_at(client, path):
    rows = client.get("/api/tree").json()
    return next(r for r in rows if r["path"] == path).get("content_changed_at")


def test_tree_content_changed_at_moves_only_on_real_content_changes(client, kb):
    kb.upsert(NodeRecord(source="agent", external_id="cc", type="note",
                         content="v1", slug="cc", parent_path="projects", title="C"))
    created = _content_changed_at(client, "projects.cc")
    assert isinstance(created, str) and created
    kb.upsert(NodeRecord(source="agent", external_id="cc", type="note",
                         content="v2", slug="cc", parent_path="projects", title="C"))
    changed = _content_changed_at(client, "projects.cc")
    assert changed > created  # ISO timestamps: later change sorts after
    kb.upsert(NodeRecord(source="agent", external_id="cc", type="note",
                         content="v2", slug="cc", parent_path="projects", title="C"))
    assert _content_changed_at(client, "projects.cc") == changed  # idempotent re-sync


def test_content_changed_at_survives_delete_and_recreate_with_same_content(client, kb):
    n = kb.upsert(NodeRecord(source="agent", external_id="cc2", type="note",
                             content="stable", slug="cc2", parent_path="projects",
                             title="C2"))
    before = _content_changed_at(client, "projects.cc2")
    kb.delete(n.id)
    kb.upsert(NodeRecord(source="agent", external_id="cc2", type="note",
                         content="stable", slug="cc2", parent_path="projects",
                         title="C2"))
    # Same bytes under a fresh node id: the churn that flattens updated_at must not
    # move the content-change timestamp.
    assert _content_changed_at(client, "projects.cc2") == before


def test_folder_content_changed_at_rolls_up_newest_subtree_change(client, kb):
    kb.upsert(NodeRecord(source="agent", external_id="f1", type="note",
                         content="one", slug="f1", parent_path="projects", title="F1"))
    kb.upsert(NodeRecord(source="agent", external_id="f2", type="note",
                         content="two", slug="f2", parent_path="projects", title="F2"))
    notes = [_content_changed_at(client, "projects.f1"),
             _content_changed_at(client, "projects.f2")]
    assert _content_changed_at(client, "projects") == max(notes)


def test_folder_rollup_matches_whole_labels_only():
    # Pure check of the batched rollup: a folder takes the newest change strictly BELOW it,
    # matched on whole ltree labels (projects.a must not absorb projects.ab), and a folder
    # whose own path changed but has no changed descendants rolls up to None.
    from datetime import datetime, timezone

    from dashboard.api.app import _mark_content_changed

    def at(day):
        return datetime(2026, 10, day, tzinfo=timezone.utc)

    changed = {"projects.a.x": at(1), "projects.a.y.z": at(3), "projects.ab.q": at(9),
               "lonely": at(5)}
    metas = [{"path": p, "type": "folder"} for p in
             ("projects", "projects.a", "projects.a.y", "projects.ab", "lonely", "empty")]
    metas.append({"path": "projects.a.x", "type": "note"})
    out = {m["path"]: m["content_changed_at"] for m in _mark_content_changed(metas, changed)}
    assert out == {
        "projects": at(9).isoformat(), "projects.a": at(3).isoformat(),
        "projects.a.y": at(3).isoformat(), "projects.ab": at(9).isoformat(),
        "lonely": None, "empty": None, "projects.a.x": at(1).isoformat(),
    }


def test_tree_is_gzipped_for_clients_that_accept_it(client, kb):
    # The tree payload can be large and is fetched on every page load; it compresses
    # well, so it must go out compressed when the browser asks.
    for i in range(30):
        kb.upsert(NodeRecord(source="agent", external_id=f"gz{i}", type="note",
                             content="body", slug=f"gz{i}", parent_path="projects",
                             title=f"Note {i}"))
    resp = client.get("/api/tree", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert len(resp.json()) >= 30   # and it still decodes to the same payload


def test_tree_clean_node_has_empty_processing(client, kb):
    _seed(kb)
    rows = client.get("/api/tree").json()
    alpha = next(r for r in rows if r["path"] == "projects.alpha")
    assert alpha["processing"] == []


def test_tree_exposes_processing_badges(client, kb):
    n = kb.upsert(NodeRecord(source="agent", external_id="p", type="note",
                             content="c", slug="p", parent_path="projects", title="P"))
    kb.flag_stale_enrichment()             # enrichment pending
    rows = client.get("/api/tree").json()
    node = next(r for r in rows if r["path"] == "projects.p")
    assert any(d["task"] == "enrichment" for d in node["processing"])


def test_provenance_groups_active_entities_with_their_source_memories(client, kb):
    from kb_core.entities import BELONGS_TO, MENTIONS

    m1 = kb.upsert(NodeRecord(source="agent", external_id="pm1", type="note",
                              content="met avery", slug="pm1", parent_path="notes", title="M1"))
    m2 = kb.upsert(NodeRecord(source="agent", external_id="pm2", type="note",
                              content="avery again", slug="pm2", parent_path="notes", title="M2"))
    avery = kb.upsert_entity("Avery", categories=["person"])   # active instance
    people = kb.upsert_entity_class("people")                    # active class
    ghost = kb.upsert_entity_class("ghosts", active=False)       # inactive -> excluded
    kb.link_entity(m1.id, avery.node.id, MENTIONS)
    kb.link_entity(m2.id, avery.node.id, MENTIONS)
    kb.link_entity(m1.id, people.node.id, BELONGS_TO)
    kb.link_entity(m1.id, ghost.node.id, BELONGS_TO)             # link exists but entity inactive

    rows = client.get("/api/provenance").json()["entities"]
    by_name = {r["name"]: r for r in rows}
    assert "ghosts" not in by_name                               # only active entities surface
    assert by_name["Avery"]["status"] == "active"

    avery_mems = by_name["Avery"]["memories"]
    assert {x["id"] for x in avery_mems} == {str(m1.id), str(m2.id)}
    assert all(x["link_type"] == MENTIONS for x in avery_mems)
    assert {x["title"] for x in avery_mems} == {"M1", "M2"}     # memory metadata travels along

    people_mems = by_name["people"]["memories"]
    assert [x["id"] for x in people_mems] == [str(m1.id)]
    assert people_mems[0]["link_type"] == BELONGS_TO


def _seed_concepts(kb):
    m1 = kb.upsert(NodeRecord(source="agent", external_id="cm1", type="note",
                              content="c", slug="cm1", parent_path="notes",
                              title="Note One"))
    m2 = kb.upsert(NodeRecord(source="agent", external_id="cm2", type="note",
                              content="c", slug="cm2", parent_path="notes",
                              title="Note Two"))
    ent = kb.upsert_entity("Ollama", categories=["tool"])
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": "c0", "prev_id": None, "parent_key": None, "level": 0,
         "name": "KB project", "kind": "project", "description": "engine work",
         "members": [(str(m1.id), "memory", 1.0), (str(m2.id), "memory", 0.8),
                     (str(ent.node.id), "entity", 0.9)],
         "embedding": None},
        {"key": "c0.0", "prev_id": None, "parent_key": "c0", "level": 1,
         "name": "Sub effort", "kind": "topic", "description": None,
         "members": [(str(m1.id), "memory", 1.0)], "embedding": None},
    ], [])
    kb.finish_concept_run(run, status="done", stats={})
    return m1, m2, ent


def test_concepts_endpoint_returns_active_layer_with_members(client, kb):
    m1, m2, ent = _seed_concepts(kb)
    body = client.get("/api/concepts").json()
    assert [c["name"] for c in body["concepts"]] == ["KB project", "Sub effort"]
    top = body["concepts"][0]
    assert top["kind"] == "project" and top["level"] == 0 and top["parent_id"] is None
    assert top["member_count"] == 3
    assert {m["label"] for m in top["members"]} == {"Note One", "Note Two", "Ollama"}
    ent_member = next(m for m in top["members"] if m["member_kind"] == "entity")
    assert ent_member["node_id"] == str(ent.node.id)
    assert top["members"][0]["score"] == 1.0          # members come strongest-first
    assert top["label_locked"] is False
    assert top["status"] == "active"
    child = body["concepts"][1]
    assert child["level"] == 1 and child["parent_id"] == top["id"]


def test_concepts_endpoint_excludes_retired_and_handles_empty(client, kb):
    assert client.get("/api/concepts").json() == {"concepts": []}
    _seed_concepts(kb)
    ids = [c["id"] for c in client.get("/api/concepts").json()["concepts"]]
    run2 = kb.begin_concept_run({})
    kb.apply_concept_run(run2, [], ids)                # retire everything
    kb.finish_concept_run(run2, status="done", stats={})
    assert client.get("/api/concepts").json() == {"concepts": []}


def test_concept_label_dismiss_and_member_endpoints(client, kb):
    m1, m2, ent = _seed_concepts(kb)
    top = client.get("/api/concepts").json()["concepts"][0]
    upd = client.post(f"/api/concepts/{top['id']}/label",
                      json={"name": "Nakatomi"}).json()
    assert upd["name"] == "Nakatomi" and upd["label_locked"] is True
    assert client.post(f"/api/concepts/{top['id']}/label",
                       json={"kind": "banana"}).status_code == 400
    rm = client.post(f"/api/concepts/{top['id']}/members/{m2.id}/remove")
    assert rm.status_code == 200
    body = client.get("/api/concepts").json()["concepts"]
    assert str(m2.id) not in {mm["node_id"] for c in body for mm in c["members"]
                              if c["id"] == top["id"]}
    dism = client.post(f"/api/concepts/{top['id']}/dismiss").json()
    assert dism["status"] == "dismissed"
    rest = client.post(f"/api/concepts/{top['id']}/restore").json()
    assert rest["status"] == "active"


def test_constraint_endpoints_roundtrip_and_contradiction(client, kb):
    m1, m2, ent = _seed_concepts(kb)
    made = client.post("/api/concepts/constraints",
                       json={"kind": "cannot_link", "left_id": str(m1.id),
                             "right_id": str(m2.id), "note": "differ"})
    assert made.status_code == 200
    cid = made.json()["id"]
    assert client.post("/api/concepts/constraints",
                       json={"kind": "must_link", "left_id": str(m1.id),
                             "right_id": str(m2.id)}).status_code == 400
    listed = client.get("/api/concepts/constraints").json()["constraints"]
    assert [c["id"] for c in listed] == [cid]
    assert client.delete(f"/api/concepts/constraints/{cid}").json()["deleted"] is True


def test_constraint_on_an_unknown_anchor_is_404_not_500(client, kb):
    """An id this tenant does not have (another tenant's, or none) used to surface
    as an unhandled ForeignKeyViolation (500)."""
    m1, _m2, _ent = _seed_concepts(kb)
    r = client.post("/api/concepts/constraints",
                    json={"kind": "cannot_link", "left_id": str(m1.id),
                          "right_id": str(uuid.uuid4())})
    assert r.status_code == 404 and r.json() == {"detail": "unknown anchor id"}
    assert client.get("/api/concepts/constraints").json()["constraints"] == []


def test_separation_endpoints_roundtrip(client, kb):
    _seed_concepts(kb)
    concepts = client.get("/api/concepts").json()["concepts"]
    top = next(c for c in concepts if c["level"] == 0)
    sub = next(c for c in concepts if c["level"] == 1)
    made = client.post("/api/concepts/separations",
                       json={"left_id": top["id"], "right_id": sub["id"],
                             "note": "unrelated"})
    assert made.status_code == 200
    sid = made.json()["id"]
    listed = client.get("/api/concepts/separations").json()["separations"]
    assert [s["id"] for s in listed] == [sid]
    assert {listed[0]["left_name"], listed[0]["right_name"]} == \
        {"KB project", "Sub effort"}
    assert client.post("/api/concepts/separations",
                       json={"left_id": top["id"],
                             "right_id": top["id"]}).status_code == 400
    assert client.delete(f"/api/concepts/separations/{sid}").json()["deleted"] is True


def test_concepts_endpoint_activity_rollup_is_opt_in(client, kb):
    _seed_concepts(kb)
    plain = client.get("/api/concepts").json()["concepts"]
    assert all("activity" not in c for c in plain)
    rolled = client.get("/api/concepts?activity_days=7").json()["concepts"]
    top = next(c for c in rolled if c["level"] == 0)
    assert top["activity"]["changed"] == 2           # both seeded memories are fresh
    assert top["activity"]["last_change"] is not None


def test_expand_endpoint_detects_id_space(client, kb):
    m1, m2, ent = _seed_concepts(kb)
    out = client.get(f"/api/expand/{ent.node.id}").json()
    assert out["kind"] == "entity" and out["label"] == "Ollama"
    assert set(out["groups"]) == {"concepts", "memories", "co_entities"}
    top = client.get("/api/concepts").json()["concepts"][0]
    out = client.get(f"/api/expand/{top['id']}?k=2").json()
    assert out["kind"] == "concept"
    assert all(len(rows) <= 2 for rows in out["groups"].values())
    assert client.get(f"/api/expand/{m1.id}").json()["kind"] == "memory"
    missing = client.get("/api/expand/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404


class _RouterStub:
    """Stands in for dashboard.api.app.InferenceRouter: hands the fake client to
    whatever task curate_parse resolves."""
    def __init__(self, fake_client):
        self._client = fake_client

    def client_for(self, task):
        return self._client


def test_curate_parse_grounds_separate_by_concept_type(client, kb, monkeypatch):
    import json as _json
    m1, m2, ent = _seed_concepts(kb)
    concepts = client.get("/api/concepts").json()["concepts"]
    top = next(c for c in concepts if c["level"] == 0)
    sub = next(c for c in concepts if c["level"] == 1)

    class _Fake:
        def complete(self, s, u, *, schema=None):
            return _json.dumps({"ops": [
                {"op": "separate", "left_concept_id": top["id"],
                 "right_concept_id": sub["id"], "note": "unrelated"},
                # an entity id in a concept-typed field must be dropped by type
                {"op": "separate", "left_concept_id": str(ent.node.id),
                 "right_concept_id": sub["id"]}],
                "warnings": []})

    monkeypatch.setattr("dashboard.api.app.InferenceRouter",
                        lambda env: _RouterStub(_Fake()))
    out = client.post("/api/concepts/curate/parse",
                      json={"text": "these are unrelated"}).json()
    assert len(out["ops"]) == 1 and out["ops"][0]["op"] == "separate"
    assert len(out["warnings"]) == 1
    assert "KB project" in out["summary"][0] and "Sub effort" in out["summary"][0]


def test_curate_parse_grounds_ops_and_fails_closed(client, kb, monkeypatch):
    import json as _json
    m1, m2, ent = _seed_concepts(kb)
    top_id = client.get("/api/concepts").json()["concepts"][0]["id"]

    class _Fake:
        def complete(self, s, u, *, schema=None):
            return _json.dumps({"ops": [
                {"op": "cannot_link", "left_id": str(ent.node.id),
                 "right_id": "00000000-0000-0000-0000-000000000000",
                 "note": "x"},
                {"op": "rename", "concept_id": "not-in-roster", "name": "Nope"},
                # a concept id in an entity-typed field (left_id) — must be dropped by
                # type, not merely by "known somewhere in the roster"
                {"op": "cannot_link", "left_id": top_id,
                 "right_id": str(ent.node.id), "note": "x"}],
                "warnings": []})

    monkeypatch.setattr("dashboard.api.app.InferenceRouter",
                        lambda env: _RouterStub(_Fake()))
    out = client.post("/api/concepts/curate/parse", json={"text": "split ollama"}).json()
    assert out["ops"] == []                              # all three ops ungrounded -> dropped
    assert len(out["warnings"]) == 3

    class _Down:
        def complete(self, s, u, *, schema=None):
            raise RuntimeError("inference down")

    monkeypatch.setattr("dashboard.api.app.InferenceRouter",
                        lambda env: _RouterStub(_Down()))
    resp = client.post("/api/concepts/curate/parse", json={"text": "anything"})
    assert resp.status_code == 503                       # fail closed, nothing written


def test_curate_parse_returns_503_on_malformed_model_output(client, kb, monkeypatch):
    import json as _json
    _seed_concepts(kb)

    class _Garbage:
        def complete(self, s, u, *, schema=None):
            return "not json at all"

    monkeypatch.setattr("dashboard.api.app.InferenceRouter",
                        lambda env: _RouterStub(_Garbage()))
    resp = client.post("/api/concepts/curate/parse", json={"text": "rename it"})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "inference unavailable"}

    class _Hallucinated:
        def complete(self, s, u, *, schema=None):
            return _json.dumps({"ops": [{"op": "frobnicate"}]})

    monkeypatch.setattr("dashboard.api.app.InferenceRouter",
                        lambda env: _RouterStub(_Hallucinated()))
    resp = client.post("/api/concepts/curate/parse", json={"text": "rename it"})
    assert resp.status_code == 503

    resp = client.post("/api/concepts/curate/parse", json={"text": "  "})
    assert resp.status_code == 400            # empty text stays a 400


def test_curate_apply_dispatches_ops(client, kb):
    m1, m2, ent = _seed_concepts(kb)
    top = client.get("/api/concepts").json()["concepts"][0]
    sub = next(c for c in client.get("/api/concepts").json()["concepts"]
               if c["level"] == 1)
    resp = client.post("/api/concepts/curate/apply", json={"ops": [
        {"op": "rename", "concept_id": top["id"], "name": "Nakatomi"},
        {"op": "cannot_link", "left_id": str(m1.id), "right_id": str(m2.id),
         "note": "differ"},
        {"op": "separate", "left_concept_id": top["id"],
         "right_concept_id": sub["id"], "note": "unrelated"},
        {"op": "dismiss", "concept_id": "00000000-0000-0000-0000-000000000000"},
        # a concept id in an entity-typed field, bypassing parse-time grounding —
        # curate_apply must catch the resulting FK violation per-op, not 500
        {"op": "cannot_link", "left_id": top["id"], "right_id": str(m1.id),
         "note": "x"},
        # an entity id in a concept-typed field must fail per-op the same way
        {"op": "separate", "left_concept_id": str(m1.id),
         "right_concept_id": sub["id"]}],
    })
    assert resp.status_code == 200
    out = resp.json()
    assert len(out["applied"]) == 3 and len(out["errors"]) == 3
    fresh = client.get("/api/concepts").json()["concepts"][0]
    assert fresh["name"] == "Nakatomi"
    assert len(client.get("/api/concepts/constraints").json()["constraints"]) == 1
    assert len(client.get("/api/concepts/separations").json()["separations"]) == 1


def test_entity_memories_paged_importance_first_with_metadata(client, kb):
    from kb_core.entities import MENTIONS
    ent = kb.upsert_entity("widget")
    ids = []
    for i, manual in enumerate([0.2, 1.5, 1.0]):
        m = kb.upsert(NodeRecord(source="agent", external_id=f"pm{i}", type="note",
                                 content="c", slug=f"pm{i}", parent_path="notes"))
        kb.set_importance(m.id, manual)
        kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.5)
        ids.append(str(m.id))
    body = client.get(f"/api/node/{ent.node.id}/memories?limit=2&offset=0").json()
    assert body["total"] == 3
    assert [m["id"] for m in body["memories"]] == [ids[1], ids[2]]  # importance desc
    row = body["memories"][0]
    assert row["link_type"] == MENTIONS
    assert row["weight"] == 0.5
    assert "event_at" in row  # iso string or null
    page2 = client.get(f"/api/node/{ent.node.id}/memories?limit=2&offset=2").json()
    assert page2["total"] == 3
    assert [m["id"] for m in page2["memories"]] == [ids[0]]


def test_entity_memories_clamps_bad_paging_params(client, kb):
    from kb_core.entities import MENTIONS
    ent = kb.upsert_entity("clampy")
    m = kb.upsert(NodeRecord(source="agent", external_id="cl", type="note",
                             content="c", slug="cl", parent_path="notes"))
    kb.link_entity(m.id, ent.node.id, MENTIONS)
    body = client.get(f"/api/node/{ent.node.id}/memories?limit=-3&offset=-9").json()
    assert body["total"] == 1
    assert len(body["memories"]) == 1  # limit clamped to 1, offset to 0


def test_non_entity_node_memories_page_is_empty(client, kb):
    n = kb.upsert(NodeRecord(source="agent", external_id="nn", type="note",
                             content="c", slug="nn", parent_path="notes"))
    assert client.get(f"/api/node/{n.id}/memories").json() == {"memories": [], "total": 0}


def test_entities_graph_carries_status(client, kb):
    kb.upsert_entity_class("alpha", active=True)
    kb.upsert_entity_class("beta", active=False)
    nodes = client.get("/api/entities").json()["nodes"]
    by_slug = {n["slug"]: n for n in nodes}
    assert by_slug["alpha"]["status"] == "active"
    assert by_slug["beta"]["status"] == "inactive"
    hub = next(n for n in nodes if n["role"] == "root")
    assert hub["status"] is None


def test_concept_run_status_is_idle_on_a_fresh_kb(client):
    body = client.get("/api/concepts/run-status").json()
    assert body["state"] == "idle"
    assert body["running"] is None and body["last"] is None
    assert body["backlog"] == 0


def test_posting_a_concept_run_queues_it(client):
    body = client.post("/api/concepts/run").json()
    assert body["state"] == "queued"
    assert body["request"]["claimed_at"] is None


def test_posting_a_concept_run_twice_is_idempotent(client, kb):
    client.post("/api/concepts/run")
    body = client.post("/api/concepts/run").json()
    assert body["state"] == "queued"
    with kb._pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM concept_run_requests").fetchone()["n"] == 1


def test_concept_run_status_reports_an_in_flight_pass(client, kb):
    with kb._pool.connection() as conn:
        conn.execute("INSERT INTO concept_runs (params, status, phase, trigger_reason) "
                     "VALUES ('{}'::jsonb, 'running', 'labeling', 'manual')")
    body = client.get("/api/concepts/run-status").json()
    assert body["state"] == "running"
    assert body["running"]["phase"] == "labeling"


def test_retrieval_traces_endpoint_lists_rows(client, kb):
    kb.log_trace(tool="search_memories", args={"query": "alpha"}, query="alpha",
                 latency_ms=42, result_count=2, confidence_level="strong")
    body = client.get("/api/retrieval/traces").json()
    assert len(body["traces"]) == 1
    t = body["traces"][0]
    assert t["tool"] == "search_memories"
    assert t["query"] == "alpha"
    assert t["judgment"] is None
    assert "args" not in t                    # payloads live on the detail route
    assert isinstance(t["at"], str)           # isoformatted


def test_retrieval_traces_summary_endpoint(client, kb):
    kb.log_trace(tool="search_memories", args={"query": "q"}, query="q",
                 latency_ms=100, result_count=1, confidence_level="weak")
    body = client.get("/api/retrieval/traces/summary?days=7").json()
    assert body["calls"] == 1
    assert body["weak_or_none"] == 1
    assert body["errors"] == 0
    assert "followup_rate" in body


def test_retrieval_summary_route_not_swallowed_by_id_route(client):
    # /traces/summary must not 404/422 by being parsed as /traces/{id}
    assert client.get("/api/retrieval/traces/summary").status_code == 200


def test_retrieval_traces_summary_empty(client, kb):
    # Carried over from the deleted /api/retrieval/telemetry/summary test: the empty
    # aggregate must still be zeros-and-nulls rather than an error or a missing key.
    body = client.get("/api/retrieval/traces/summary").json()
    assert body["calls"] == 0
    assert body["latency_p50_ms"] is None


def test_trace_detail_and_judgment_roundtrip(client, kb):
    kb.log_trace(tool="search_memories", args={"query": "q"}, query="q",
                 latency_ms=10, result_count=0, results=[])
    tid = kb.trace_recent()[0]["id"]
    detail = client.get(f"/api/retrieval/traces/{tid}").json()
    assert detail["args"] == {"query": "q"} and detail["judgments"] == []
    assert detail["turn_id"] == tid
    resp = client.post(f"/api/retrieval/traces/{tid}/judgment",
                       json={"verdict": "good", "notes": "spot on"})
    assert resp.status_code == 200
    # judged_by is config-driven (KB_DASHBOARD_IDENTITY); "owner" is the unset default.
    assert resp.json()["judged_by"] == "owner"
    assert client.get(f"/api/retrieval/traces/{tid}").json()["judgments"][0]["verdict"] == "good"


def test_trace_detail_404_and_judgment_400(client, kb):
    assert client.get("/api/retrieval/traces/999999").status_code == 404
    kb.log_trace(tool="get_memory", args={}, latency_ms=1)
    tid = kb.trace_recent()[0]["id"]
    assert client.post(f"/api/retrieval/traces/{tid}/judgment",
                       json={"verdict": "meh"}).status_code == 400
    assert client.post("/api/retrieval/traces/999999/judgment",
                       json={"verdict": "good"}).status_code == 404


def test_retrieval_heatmap_endpoint_returns_rows(client, kb):
    _seed(kb)
    alpha = kb.get_by_path("projects.alpha")
    kb.log_trace(tool="search_memories", args={}, latency_ms=5, result_count=1,
                 results=[{"id": str(alpha.id), "kind": "memory", "rank": 1}])
    body = client.get("/api/retrieval/heatmap?days=7&bucket=day&tz=UTC&path=projects").json()
    assert len(body["buckets"]) == 7
    assert [r["title"] for r in body["rows"]] == ["Alpha"]
    assert body["rows"][0]["surfaced"] == 1 and body["rows"][0]["opened"] == 0


def test_retrieval_heatmap_endpoint_days_all_and_defaults(client, kb):
    body = client.get("/api/retrieval/heatmap?days=all").json()
    assert body["window"]["days"] is None
    assert body["window"]["bucket"] == "day" and body["window"]["tz"] == "UTC"
    assert body["rows"] == []


def test_retrieval_heatmap_endpoint_clamps_days(client, kb):
    assert client.get("/api/retrieval/heatmap?days=9999").json()["window"]["days"] == 365


def test_retrieval_heatmap_endpoint_rejects_bad_input(client, kb):
    assert client.get("/api/retrieval/heatmap?bucket=month").status_code == 422
    assert client.get("/api/retrieval/heatmap?tz=Nowhere/Land").status_code == 422
    assert client.get("/api/retrieval/heatmap?days=soon").status_code == 422
    assert client.get("/api/retrieval/heatmap?path=a;b").status_code == 422


def test_mcp_tools_lists_published_inventory(kb):
    kb.publish_tool_inventory([("search_memories", "read"), ("add_memory", "write")])
    client = TestClient(create_app(kb, db_viewer_password="pw"))
    body = client.get("/api/mcp/tools").json()
    assert [t["tool"] for t in body["tools"]] == ["search_memories", "add_memory"]
    assert body["tools"][0]["kind"] == "read"
    assert body["tools"][0]["enabled"] is True
    # updated_at is isoformatted for the browser, never a datetime.
    assert isinstance(body["tools"][0]["updated_at"], str)


def test_mcp_tools_empty_before_publish(kb):
    client = TestClient(create_app(kb, db_viewer_password="pw"))
    assert client.get("/api/mcp/tools").json() == {"tools": []}


def test_toggle_requires_the_db_password(kb):
    kb.publish_tool_inventory([("add_memory", "write")])
    client = TestClient(create_app(kb, db_viewer_password="pw"))
    unauthorized = client.post("/api/mcp/tools/add_memory", json={"enabled": False})
    assert unauthorized.status_code == 401
    # And nothing changed.
    assert kb.tool_states()[0]["enabled"] is True


def test_toggle_flips_the_tool(kb):
    kb.publish_tool_inventory([("add_memory", "write")])
    client = TestClient(create_app(kb, db_viewer_password="pw"))
    resp = client.post("/api/mcp/tools/add_memory", json={"enabled": False},
                       headers={"X-DB-Password": "pw"})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    # updated_by is config-driven (KB_DASHBOARD_IDENTITY); "owner" is the unset default.
    assert resp.json()["updated_by"] == "owner"
    assert kb.tool_states()[0]["enabled"] is False


def test_toggle_unknown_tool_is_404(kb):
    client = TestClient(create_app(kb, db_viewer_password="pw"))
    resp = client.post("/api/mcp/tools/nope", json={"enabled": False},
                       headers={"X-DB-Password": "pw"})
    assert resp.status_code == 404


def test_toggle_without_configured_password_is_503(kb):
    kb.publish_tool_inventory([("add_memory", "write")])
    client = TestClient(create_app(kb))  # no db_viewer_password
    resp = client.post("/api/mcp/tools/add_memory", json={"enabled": False},
                       headers={"X-DB-Password": "pw"})
    assert resp.status_code == 503


def test_claude_auth_status_endpoint_returns_level(client, monkeypatch):
    import dashboard.api.app as appmod
    monkeypatch.setattr(appmod, "claude_auth_status",
                        lambda: {"level": "warn", "logged_in": True, "days_left": 5.0,
                                 "expires_at": "2026-09-07T19:32:44+00:00",
                                 "email": "m@example.com"})
    body = client.get("/api/auth/claude").json()
    assert body["level"] == "warn" and body["days_left"] == 5.0


def test_relogin_is_off_by_default(kb, monkeypatch):
    # Opt-in: a fresh install must not expose a route that starts a process on the
    # host. Off, it is a 404 even with the right password, and it never runs anything.
    import dashboard.api.app as appmod
    monkeypatch.setattr(appmod.subprocess, "run",
                        lambda *a, **kw: pytest.fail("relogin ran while disabled"))
    c = TestClient(create_app(kb, db_viewer_password="pw"))
    assert c.post("/api/auth/claude/relogin",
                  headers={"X-DB-Password": "pw"}).status_code == 404


def test_disabled_relogin_is_not_a_password_oracle(kb):
    # The flag is checked before the password: a disabled route must not say "401
    # wrong" vs "404" depending on the guess, nor spend the lockout budget.
    c = TestClient(create_app(kb, db_viewer_password="pw"))
    for _ in range(10):
        assert c.post("/api/auth/claude/relogin",
                      headers={"X-DB-Password": "nope"}).status_code == 404
    assert c.post("/api/db/auth", headers={"X-DB-Password": "pw"}).status_code == 200


def test_claude_auth_status_reports_whether_relogin_is_enabled(kb, monkeypatch):
    import dashboard.api.app as appmod
    monkeypatch.setattr(appmod, "claude_auth_status",
                        lambda: {"level": "warn", "logged_in": True, "days_left": 5.0,
                                 "expires_at": None, "email": None})
    assert TestClient(create_app(kb)).get(
        "/api/auth/claude").json()["relogin_enabled"] is False
    assert TestClient(create_app(kb, allow_relogin=True)).get(
        "/api/auth/claude").json()["relogin_enabled"] is True


@pytest.mark.parametrize("value, expected", [
    (None, False), ("", False), ("0", False), ("no", False), ("false", False),
    ("1", True), ("true", True), ("YES", True), (" on ", True)])
def test_relogin_env_flag(value, expected):
    import dashboard.api.app as appmod
    env = {} if value is None else {"KB_DASHBOARD_ALLOW_RELOGIN": value}
    assert appmod._env_flag(env, "KB_DASHBOARD_ALLOW_RELOGIN") is expected


def test_relogin_is_503_when_no_password_configured(kb):
    # Fail closed: an unconfigured gate must never leave a process-spawning
    # endpoint open, even when the route is switched on.
    c = TestClient(create_app(kb, allow_relogin=True))
    assert c.post("/api/auth/claude/relogin").status_code == 503


def test_relogin_rejects_missing_and_wrong_password(kb):
    c = TestClient(create_app(kb, db_viewer_password="pw", allow_relogin=True))
    assert c.post("/api/auth/claude/relogin").status_code == 401
    assert c.post("/api/auth/claude/relogin",
                  headers={"X-DB-Password": "nope"}).status_code == 401


def test_relogin_launches_terminal_with_a_fixed_command(kb, monkeypatch):
    calls = []
    import dashboard.api.app as appmod

    def fake_run(*a, **kw):
        calls.append(a[0])
        return subprocess.CompletedProcess(a[0], returncode=0)

    monkeypatch.setattr(appmod.subprocess, "run", fake_run)
    c = TestClient(create_app(kb, db_viewer_password="pw", allow_relogin=True))
    r = c.post("/api/auth/claude/relogin", headers={"X-DB-Password": "pw"})
    assert r.status_code == 200 and r.json() == {"launched": True}
    # The command is a fixed literal — no request data reaches the shell.
    assert calls == [["osascript", "-e",
                      'tell application "Terminal" to do script "claude auth login"']]


def test_relogin_fails_when_osascript_cannot_open_terminal(kb, monkeypatch):
    # Under launchd, driving Terminal.app requires AppleEvents/Automation TCC
    # consent a headless launchd context never has. osascript then exits
    # non-zero (e.g. -1743) and nothing actually opened — the endpoint must
    # not report {"launched": True} in that case, or the dashboard tells the
    # user Terminal opened when it didn't.
    import dashboard.api.app as appmod
    monkeypatch.setattr(
        appmod.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=1))
    c = TestClient(create_app(kb, db_viewer_password="pw", allow_relogin=True))
    r = c.post("/api/auth/claude/relogin", headers={"X-DB-Password": "pw"})
    assert r.status_code >= 300
    assert r.json() != {"launched": True}


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _limited(kb, clock, **kw):
    from dashboard.api.app import FailedAttemptLimiter
    lim = FailedAttemptLimiter(max_failures=3, window_s=60, lockout_s=600, clock=clock, **kw)
    return TestClient(create_app(kb, db_viewer_password="pw", db_password_limiter=lim)), lim


def test_db_password_locks_out_after_repeated_wrong_guesses(kb):
    clock = _Clock()
    c, _ = _limited(kb, clock)
    for _ in range(3):
        assert c.post("/api/db/auth", headers={"X-DB-Password": "nope"}).status_code == 401
    r = c.post("/api/db/auth", headers={"X-DB-Password": "nope"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) == 600
    # Locked means locked: the right password is refused too, or the lock would still
    # answer right-vs-wrong and guessing would continue at full speed.
    assert c.post("/api/db/auth", headers={"X-DB-Password": "pw"}).status_code == 429
    # Every gated route shares the one lock.
    assert c.get("/api/db/tables", headers={"X-DB-Password": "pw"}).status_code == 429
    clock.now += 599
    r = c.post("/api/db/auth", headers={"X-DB-Password": "pw"})
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"
    clock.now += 1
    assert c.post("/api/db/auth", headers={"X-DB-Password": "pw"}).status_code == 200


def test_db_password_failures_outside_the_window_do_not_add_up(kb):
    clock = _Clock()
    c, _ = _limited(kb, clock)
    for _ in range(5):
        assert c.post("/api/db/auth", headers={"X-DB-Password": "nope"}).status_code == 401
        clock.now += 31            # never 3 inside one 60 s window
    assert c.post("/api/db/auth", headers={"X-DB-Password": "pw"}).status_code == 200


def test_db_password_success_clears_the_failure_count(kb):
    clock = _Clock()
    c, _ = _limited(kb, clock)
    for _ in range(2):
        c.post("/api/db/auth", headers={"X-DB-Password": "nope"})
    assert c.post("/api/db/auth", headers={"X-DB-Password": "pw"}).status_code == 200
    for _ in range(2):
        assert c.post("/api/db/auth", headers={"X-DB-Password": "nope"}).status_code == 401
    assert c.post("/api/db/auth", headers={"X-DB-Password": "pw"}).status_code == 200


def test_missing_db_password_header_is_not_counted_as_a_guess(kb):
    # The UI sends an empty header from a tab that never unlocked; that must not lock
    # the owner out.
    clock = _Clock()
    c, _ = _limited(kb, clock)
    for _ in range(10):
        assert c.get("/api/db/tables").status_code == 401
        assert c.get("/api/db/tables", headers={"X-DB-Password": ""}).status_code == 401
    assert c.post("/api/db/auth", headers={"X-DB-Password": "pw"}).status_code == 200


def test_db_password_limiter_reset_and_app_state(kb):
    clock = _Clock()
    c, lim = _limited(kb, clock)
    assert c.app.state.db_password_limiter is lim
    for _ in range(3):
        c.post("/api/db/auth", headers={"X-DB-Password": "nope"})
    assert c.post("/api/db/auth", headers={"X-DB-Password": "pw"}).status_code == 429
    lim.reset()
    assert c.post("/api/db/auth", headers={"X-DB-Password": "pw"}).status_code == 200


def test_db_password_lockout_is_per_client_address():
    from dashboard.api.app import FailedAttemptLimiter
    clock = _Clock()
    lim = FailedAttemptLimiter(max_failures=2, window_s=60, lockout_s=600, clock=clock)
    lim.record_failure("10.0.0.1")
    lim.record_failure("10.0.0.1")
    assert lim.retry_after("10.0.0.1") == 600
    assert lim.retry_after("10.0.0.2") is None


def test_db_password_limiter_memory_is_bounded():
    from dashboard.api.app import FailedAttemptLimiter
    clock = _Clock()
    lim = FailedAttemptLimiter(max_failures=5, window_s=60, lockout_s=600, clock=clock)
    lim._MAX_KEYS = 100
    for i in range(1000):
        lim.record_failure(f"10.0.{i // 256}.{i % 256}")
    assert len(lim._failures) <= 100


def test_unconfigured_db_password_is_503_before_any_lockout(kb):
    from dashboard.api.app import FailedAttemptLimiter
    lim = FailedAttemptLimiter(max_failures=1, clock=_Clock())
    c = TestClient(create_app(kb, db_password_limiter=lim))
    for _ in range(3):
        assert c.post("/api/db/auth", headers={"X-DB-Password": "x"}).status_code == 503


@pytest.mark.parametrize("env, warns", [
    ({}, False),
    ({"KB_DASHBOARD_ORIGINS": "http://localhost:5173,http://127.0.0.1:8091"}, False),
    ({"KB_API_ALLOWED_HOSTS": "localhost:8000,127.0.0.1,testserver"}, False),
    ({"KB_DASHBOARD_ORIGINS": "https://kb.example.com"}, True),
    ({"KB_FRONTEND_ALLOWED_HOSTS": "kb.example.com"}, True),
    ({"KB_API_ALLOWED_HOSTS": "127.0.0.1,kb.example.com:443"}, True),
    ({"KB_FRONTEND_ALLOWED_HOSTS": "kb.example.com", "KB_DASHBOARD_AUTH": "cf-access"}, False),
    ({"KB_FRONTEND_ALLOWED_HOSTS": "kb.example.com", "KB_DASHBOARD_AUTH": "none"}, True),
])
def test_exposure_problem_when_public_host_has_no_in_app_auth(env, warns):
    import dashboard.api.app as appmod
    msg = appmod._exposure_problem(env)
    assert (msg is not None) is warns
    if warns:
        assert "kb.example.com" in msg and "KB_DASHBOARD_AUTH=cf-access" in msg


def test_ingest_status_carries_active_provider_pauses(client, kb):
    from datetime import datetime, timedelta, timezone
    body = client.get("/api/ingest/status").json()
    assert body["pauses"] == []
    kb.pause_provider("claude-cli",
                      until=datetime.now(timezone.utc) + timedelta(hours=1),
                      reason="session limit")
    [p] = client.get("/api/ingest/status").json()["pauses"]
    assert p["provider"] == "claude-cli" and p["reason"] == "session limit"
    assert isinstance(p["until"], str) and "T" in p["until"]     # ISO timestamp


def test_aging_toggle_roundtrip(client):
    assert client.get("/api/aging").json() == {"enabled": True}
    r = client.post("/api/aging", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.get("/api/aging").json()["enabled"] is False
    client.post("/api/aging", json={"enabled": True})    # restore for neighbors


def test_ingest_status_lists_stuck_records(client, kb):
    kb.log_ingest_failed("obsidian", "notes/a.md", "boom", path="notes.a")
    body = client.get("/api/ingest/status").json()
    assert len(body["stuck"]) == 1
    s = body["stuck"][0]
    assert s["source"] == "obsidian"
    assert s["external_id"] == "notes/a.md"
    assert s["node_id"] == str(node_id("obsidian", "notes/a.md"))
    assert s["path"] == "notes.a"
    assert s["error"] == "boom"
    assert isinstance(s["since"], str)                       # ISO timestamp, JSON-safe


def test_ingest_status_stuck_is_empty_when_nothing_is_stuck(client, kb):
    assert client.get("/api/ingest/status").json()["stuck"] == []


def test_dismiss_stuck_record_clears_it(client, kb):
    kb.log_ingest_failed("obsidian", "notes/a.md", "boom")
    resp = client.post("/api/ingest/stuck/dismiss",
                       json={"source": "obsidian", "external_id": "notes/a.md"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert client.get("/api/ingest/status").json()["stuck"] == []
    assert kb.ingest_log(event="ingest_resolved")[0]["detail"] == {"reason": "dismissed"}


def test_dismiss_requires_source_and_external_id(client, kb):
    assert client.post("/api/ingest/stuck/dismiss", json={"source": "obsidian"}).status_code == 422


def test_dismiss_a_record_that_is_not_stuck_reports_ok_false(client, kb):
    resp = client.post("/api/ingest/stuck/dismiss",
                       json={"source": "obsidian", "external_id": "never-failed"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": False}
    assert kb.ingest_log(event="ingest_resolved") == []


def test_policy_endpoint_reports_the_loaded_policy(client):
    body = client.get("/api/policy").json()
    assert body["source"] == "<legacy fixture>" and body["is_default"] is False
    assert body["overlays"] == []
    assert [r["name"] for r in body["policy"]["decay"]] == ["email"]
    assert body["policy"]["decay"][0] == {"name": "email", "match": {"tags": ["email"]},
                                          "half_life_days": 17.0, "floor": 0.2}
    assert body["policy"]["projects"]["folder_label"] == "Projects"
    assert body["policy"]["importance"]["recency_floor"] == 0.65


def test_policy_endpoint_for_shipped_defaults(pool):
    from kb_core.knowledge_base import KnowledgeBase
    from kb_core.policy import Policy
    from kb_core.registry import default_registry
    plain = TestClient(create_app(KnowledgeBase(pool, default_registry(), policy=Policy())))
    body = plain.get("/api/policy").json()
    assert body["source"] is None and body["is_default"] is True
    assert body["policy"]["decay"] == [] and body["policy"]["projects"] is None


def test_trace_replay_route_serves_full_and_partial(client, kb):
    kb.log_trace(tool="search_memories", args={"query": "q", "limit": 5}, query="q",
                 latency_ms=10, result_count=0, results=[])
    tid = kb.trace_recent()[0]["id"]
    body = client.get(f"/api/retrieval/traces/{tid}/replay").json()
    assert body["completeness"] == "partial"
    assert body["replay"] == {"v": 1, "limit": 5, "flat": {"fused": []}}
    assert isinstance(body["at"], str)


def test_trace_replay_route_404s_for_unknown_and_non_search(client, kb):
    assert client.get("/api/retrieval/traces/999999/replay").status_code == 404
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=1)
    tid = kb.trace_recent()[0]["id"]
    assert client.get(f"/api/retrieval/traces/{tid}/replay").status_code == 404


def test_trace_list_and_detail_carry_has_replay_not_the_payload(client, kb):
    kb.log_trace(tool="search_graph", args={"query": "q"}, query="q", latency_ms=1,
                 replay={"v": 1, "limit": 1, "flat": {"fused": []}})
    t = client.get("/api/retrieval/traces").json()["traces"][0]
    assert t["has_replay"] is True and "replay" not in t
    detail = client.get(f"/api/retrieval/traces/{t['id']}").json()
    assert detail["has_replay"] is True and "replay" not in detail


def test_retrieval_turns_endpoint_lists_and_details(client, kb):
    kb.log_trace(tool="search_memories", args={"query": "alpha"}, query="alpha",
                 latency_ms=42, result_count=2, confidence_level="strong", session_id="s1")
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=3, session_id="s1",
                 result_count=1)
    body = client.get("/api/retrieval/turns").json()
    assert len(body["turns"]) == 1
    t = body["turns"][0]
    assert t["calls"] == 2 and t["first_query"] == "alpha" and t["outcome"] == "returned"
    assert t["tools"] == {"search_memories": 1, "get_memory": 1}
    assert isinstance(t["started_at"], str) and isinstance(t["ended_at"], str)
    detail = client.get(f"/api/retrieval/turns/{t['id']}").json()
    assert [c["tool"] for c in detail["traces"]] == ["search_memories", "get_memory"]
    assert isinstance(detail["traces"][0]["at"], str)
    assert isinstance(detail["traces"][0]["request_id"], str)
    assert detail["traces"][0]["turn_id"] == t["id"]
    assert detail["judgments"] == []


def test_retrieval_turn_routes_404_for_non_openers(client, kb):
    kb.log_trace(tool="search_memories", args={"query": "a"}, query="a", latency_ms=1,
                 session_id="s1")
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=1, session_id="s1")
    # The opener resolves on both routes, so a 404 below can only come from the
    # opener check, not from a broken route.
    opener = client.get("/api/retrieval/turns").json()["turns"][0]["id"]
    assert client.get(f"/api/retrieval/turns/{opener}").status_code == 200
    assert client.post(f"/api/retrieval/turns/{opener}/judgment",
                       json={"verdict": "good"}).status_code == 200
    follower = client.get("/api/retrieval/traces").json()["traces"][0]["id"]
    assert follower != opener
    assert client.get(f"/api/retrieval/turns/{follower}").status_code == 404
    assert client.post(f"/api/retrieval/turns/{follower}/judgment",
                       json={"verdict": "good"}).status_code == 404
    assert client.get("/api/retrieval/turns/999999").status_code == 404


def test_retrieval_turn_judgment_roundtrip_and_400(client, kb):
    kb.log_trace(tool="search_memories", args={"query": "a"}, query="a", latency_ms=1,
                 session_id="s1")
    tid = client.get("/api/retrieval/turns").json()["turns"][0]["id"]
    assert client.post(f"/api/retrieval/turns/{tid}/judgment",
                       json={"verdict": "meh"}).status_code == 400
    resp = client.post(f"/api/retrieval/turns/{tid}/judgment",
                       json={"verdict": "good", "notes": "useful"})
    assert resp.status_code == 200
    row = resp.json()
    assert row["turn_id"] == tid and row["verdict"] == "good" and row["notes"] == "useful"
    assert row["judged_by"] and isinstance(row["at"], str)
    assert client.get("/api/retrieval/turns").json()["turns"][0]["judgment"] == "good"
    assert client.get("/api/retrieval/turns?judged=false").json()["turns"] == []
    assert client.get(f"/api/retrieval/turns/{tid}").json()["judgments"][0]["verdict"] == "good"


def test_retrieval_traces_rows_carry_turn_id_and_summary_carries_turn_fields(client, kb):
    kb.log_trace(tool="search_memories", args={"query": "a"}, query="a", latency_ms=1,
                 session_id="s1")
    t = client.get("/api/retrieval/traces").json()["traces"][0]
    assert t["turn_id"] == t["id"]
    s = client.get("/api/retrieval/traces/summary").json()
    assert s["turns"] == 1 and s["turn_gap_s"] == 90
    assert "calls_per_turn_median" in s and "tokens_per_turn_median" in s


def test_change_hub_stop_survives_a_swallowed_cancel():
    """On Python < 3.12 psycopg's notifies() can swallow a cancel (asyncio.wait_for drops
    one that lands as a notification completes the wait), so the LISTEN loop runs on and
    a single `cancel(); await task` hung lifespan shutdown forever. stop() must re-cancel
    until the listener ends -- and give up rather than hang if it never does."""
    import asyncio

    from dashboard.api import app as dash_app

    async def stubborn(swallow: int, seen: list) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                seen.append(1)
                if len(seen) > swallow:
                    raise

    async def scenario(swallow: int) -> int:
        hub = dash_app.ChangeHub("unused")
        seen: list = []
        hub._task = asyncio.create_task(stubborn(swallow, seen))
        await asyncio.sleep(0)
        await asyncio.wait_for(hub.stop(), timeout=10)
        return len(seen)

    def run_bounded(swallow: int) -> int:
        # A stop() that awaits the listener unboundedly never returns (awaiting a task
        # that ignores cancels waits on it forever, even under wait_for), so the scenario
        # runs on its own thread and a hang fails the test instead of stalling the suite.
        import threading
        out: list = []
        t = threading.Thread(target=lambda: out.append(asyncio.run(scenario(swallow))),
                             daemon=True)
        t.start()
        t.join(30)
        assert not t.is_alive(), "ChangeHub.stop() hung on a listener that swallowed a cancel"
        return out[0]

    assert run_bounded(2) == 3                          # two cancels swallowed, the third lands
    # one that swallows every cancel stop() sends is abandoned after the bound (asyncio.run's
    # own final cancel then ends it)
    assert run_bounded(dash_app._STOP_ATTEMPTS) == dash_app._STOP_ATTEMPTS


def test_claude_auth_banner_stays_quiet_when_nothing_routes_to_claude_cli(
        client, monkeypatch, tmp_path):
    """An Ollama-only install showed 'Claude auth is signed out — enrichment is
    stranding' and shelled out to `claude` on every poll."""
    import dashboard.api.app as appmod
    from kb_core.inference_routing import TASKS
    p = tmp_path / "inference.toml"
    p.write_text("".join(f'[task.{n}]\nprovider = "local"\n' for n in TASKS))
    monkeypatch.setenv("KB_INFERENCE_ROUTING", str(p))
    monkeypatch.setattr(appmod, "claude_auth_status",
                        lambda: pytest.fail("probed claude auth with no claude-cli task"))
    body = client.get("/api/auth/claude").json()
    assert body["level"] == "ok" and body["in_use"] is False


def test_claude_auth_banner_reports_when_a_task_routes_to_claude_cli(
        client, monkeypatch, tmp_path):
    import dashboard.api.app as appmod
    from kb_core.inference_routing import TASKS
    p = tmp_path / "inference.toml"
    p.write_text("".join(
        f'[task.{n}]\nprovider = "{"claude-cli" if n == "extract_entities" else "local"}"\n'
        for n in TASKS))
    monkeypatch.setenv("KB_INFERENCE_ROUTING", str(p))
    monkeypatch.setattr(appmod, "claude_auth_status",
                        lambda: {"level": "out", "logged_in": False, "days_left": None,
                                 "expires_at": None, "email": None})
    body = client.get("/api/auth/claude").json()
    assert body["level"] == "out" and body["in_use"] is True


# --- KB_DB_VIEWER_PASSWORD strength -------------------------------------------------

@pytest.mark.parametrize("value", ["change-me", "CHANGE-ME-please-123456", "password",
                                   "<openssl rand -hex 16>", "your-password-goes-here",
                                   "short-but-ok", "aaaaaaaaaaaaaaaaaaaa"])
def test_weak_db_viewer_passwords_are_refused(value):
    import dashboard.api.app as appmod
    assert appmod.db_viewer_password_problem(value) is not None


@pytest.mark.parametrize("value", [None, "", "3f9c1a7be2d84c06", "a-long-passphrase-of-words"])
def test_unset_or_strong_db_viewer_passwords_pass(value):
    import dashboard.api.app as appmod
    assert appmod.db_viewer_password_problem(value) is None


def test_main_keeps_the_gate_locked_with_a_weak_password(kb, capsys):
    """A weak value is not used: the gated endpoints answer exactly as when unset (503)."""
    import dashboard.api.app as appmod
    assert appmod._db_viewer_password({"KB_DB_VIEWER_PASSWORD": "change-me"}) is None
    assert "placeholder" in capsys.readouterr().err
    strong = "3f9c1a7be2d84c06"
    assert appmod._db_viewer_password({"KB_DB_VIEWER_PASSWORD": strong}) == strong
    c = TestClient(create_app(kb, db_viewer_password=appmod._db_viewer_password(
        {"KB_DB_VIEWER_PASSWORD": "change-me"})))
    assert c.get("/api/db/tables", headers={"X-DB-Password": "change-me"}).status_code == 503
