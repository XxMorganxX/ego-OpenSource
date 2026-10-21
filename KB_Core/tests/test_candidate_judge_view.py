from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS


def _mem(kb, eid, title, content, parent_path=""):
    return kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                                title=title, content=content, slug=eid,
                                parent_path=parent_path)).id


def test_view_returns_raw_name_and_mention_snippets(kb):
    kb.stage_entity_candidate(
        _mem(kb, "m0", "Design doc", "lantern_sync drives the nightly CRM export"),
        "tool", "lantern_sync")
    kb.stage_entity_candidate(
        _mem(kb, "m1", "Runbook", "we ran lantern_sync nightly"),
        "tool", "lantern_sync")

    view = kb.candidate_judge_view("tool", "lantern_sync")
    assert view["name"] == "lantern_sync"
    assert len(view["mentions"]) == 2
    joined = " ".join(view["mentions"])
    assert "nightly CRM export" in joined and "ran lantern_sync nightly" in joined


def test_candidate_view_prefixes_mentions_with_filed_under_folder(kb):
    """Where a mentioning memory is filed is deliberate judge signal — nested notes get a
    '[folder › breadcrumb]' prefix; a top-level note gets none."""
    kb.stage_entity_candidate(
        _mem(kb, "n0", "Design doc", "build_pick_list builds the picking route",
             parent_path="projects.kb_core"),
        "tool", "build_pick_list")
    kb.stage_entity_candidate(
        _mem(kb, "n1", "Scratch", "build_pick_list again"),
        "tool", "build_pick_list")

    mentions = kb.candidate_judge_view("tool", "build_pick_list")["mentions"]
    assert any(m.startswith("[projects › kb_core] Design doc:") for m in mentions)
    assert any(m.startswith("Scratch:") for m in mentions)      # top-level -> no prefix


def test_entity_judge_view_prefixes_mentions_with_filed_under_folder(kb):
    """Same prefix on the promoted-entity view that feeds describe / merge judge."""
    mem = _mem(kb, "n2", "Reunion notes", "Acme reunion in June",
               parent_path="people.alumni")
    ent = kb.upsert_entity("Acme", categories=["organization"])
    kb.link_entity(mem, ent.node.id, MENTIONS)

    view = kb._entity_judge_view(ent.node.id)
    assert view["mentions"] == ["[people › alumni] Reunion notes: Acme reunion in June"]
