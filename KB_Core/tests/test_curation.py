"""The curation layer: commands that compose KnowledgeBase primitives into previewable,
applyable taxonomy edits returning a shared ImpactReport."""

import pytest

from kb_core.curation import CurationConflict, CurationService


def test_commands_lists_registered_actions_with_arg_schemas(kb):
    svc = CurationService(kb)
    by_action = {c["action"]: c for c in svc.commands()}
    assert {"create", "pin", "unpin"} <= set(by_action)
    assert by_action["create"]["args"] == ["name"]
    assert by_action["create"]["reversible"] is True


def test_create_previews_then_applies_a_pinned_class(kb):
    svc = CurationService(kb)
    preview = svc.preview("create", name="hobbies")
    assert preview.applied is False
    assert preview.action == "create"
    result = svc.apply("create", name="hobbies")
    assert result.applied is True
    cls = kb.get_entity_class("hobbies")
    assert cls.status == "active" and cls.pinned is True


def test_create_rejects_invalid_name(kb):
    svc = CurationService(kb)
    with pytest.raises(ValueError):
        svc.preview("create", name="category")  # placeholder/meta word


def test_create_on_blocked_name_warns_and_conflicts(kb):
    kb.upsert_entity_class("spam", active=True)
    kb.block_class("spam")
    svc = CurationService(kb)
    assert any("blocked" in w for w in svc.preview("create", name="spam").warnings)
    with pytest.raises(CurationConflict):
        svc.apply("create", name="spam")


def test_pin_and_unpin_toggle_the_flag(kb):
    kb.upsert_entity_class("people", active=True)
    svc = CurationService(kb)
    svc.apply("pin", name="people")
    assert kb.get_entity_class("people").pinned is True
    svc.apply("unpin", name="people")
    assert kb.get_entity_class("people").pinned is False


def test_unknown_action_raises_value_error(kb):
    with pytest.raises(ValueError):
        CurationService(kb).preview("frobnicate", name="x")


from kb_core.domain import NodeRecord
from kb_core.entities import BELONGS_TO


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


def test_prune_preview_reports_impact_and_apply_blocks(kb):
    cls = kb.upsert_entity_class("frameworks", active=True)
    mem = kb.upsert(_mem("m1"))
    kb.link_entity(mem.id, cls.node.id, BELONGS_TO)
    svc = CurationService(kb)
    preview = svc.preview("prune", name="frameworks")
    assert preview.reversible is False
    assert preview.queued_reenrichment == 1
    result = svc.apply("prune", name="frameworks")
    assert result.applied is True
    assert kb.get_entity_class("frameworks").status == "blocked"


def test_prune_preview_warns_when_pinned(kb):
    kb.upsert_entity_class("people", active=True)
    kb.set_class_pinned("people", True)
    svc = CurationService(kb)
    assert any("pinned" in w for w in svc.preview("prune", name="people").warnings)


def test_prune_missing_class_raises_lookup(kb):
    with pytest.raises(LookupError):
        CurationService(kb).preview("prune", name="ghost")


def test_set_description_applies_and_locks(kb):
    kb.upsert_entity_class("people", active=True)
    svc = CurationService(kb)
    svc.apply("set_description", name="people", description="humans we know")
    cls = next(r for r in kb.list_entity_classes() if r["name"] == "people")
    assert cls["description"] == "humans we know"
    assert cls["description_locked"] is True


def test_unblock_returns_class_to_inactive(kb):
    kb.upsert_entity_class("spam", active=True)
    kb.block_class("spam")
    svc = CurationService(kb)
    svc.apply("unblock", name="spam")
    assert kb.get_entity_class("spam").status == "inactive"


def test_merge_preview_then_apply_folds_source_into_target(kb):
    src = kb.upsert_entity_class("frameworks", active=True)
    kb.upsert_entity_class("tools", active=True)
    mem = kb.upsert(_mem("m1"))
    kb.link_entity(mem.id, src.node.id, BELONGS_TO)
    kb.mark_enriched(mem.id)
    svc = CurationService(kb)
    preview = svc.preview("merge", source="frameworks", target="tools")
    assert preview.action == "merge" and preview.reversible is False
    assert preview.queued_reenrichment == 1
    result = svc.apply("merge", source="frameworks", target="tools")
    assert result.applied is True
    assert kb.get_entity_class("frameworks").status == "blocked"
    assert kb.get_entity_class("tools").pinned is True            # target pinned
    assert kb.get(mem.id).enrichment_status == "pending"          # voters requeued


def test_merge_into_self_is_rejected(kb):
    kb.upsert_entity_class("tools", active=True)
    with pytest.raises(ValueError):
        CurationService(kb).preview("merge", source="tools", target="tools")


def test_merge_into_blocked_target_conflicts(kb):
    kb.upsert_entity_class("frameworks", active=True)
    kb.upsert_entity_class("tools", active=True)
    kb.block_class("tools")
    svc = CurationService(kb)
    assert any("blocked" in w for w in
               svc.preview("merge", source="frameworks", target="tools").warnings)
    with pytest.raises(CurationConflict):
        svc.apply("merge", source="frameworks", target="tools")


def test_rename_is_a_merge_into_the_new_name(kb):
    src = kb.upsert_entity_class("framework", active=True)
    mem = kb.upsert(_mem("m1"))
    kb.link_entity(mem.id, src.node.id, BELONGS_TO)
    svc = CurationService(kb)
    result = svc.apply("rename", source="framework", target="frameworks")
    assert result.action == "rename" and result.applied is True
    assert kb.get_entity_class("framework").status == "blocked"
    assert kb.get_entity_class("frameworks").pinned is True
