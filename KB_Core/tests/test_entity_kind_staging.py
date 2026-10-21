"""Kind/role/occurred_at flow through staging; class-name typo gate at the KB boundary."""
from datetime import datetime, timezone

from kb_core.domain import NodeRecord


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


def test_stage_candidate_carries_occurred_at_and_role(kb):
    m = kb.upsert(_mem("m1"))
    ts = datetime(2026, 7, 15, tzinfo=timezone.utc)
    kb.stage_entity_candidate(m.id, "event", "Vandelay kickoff sync",
                              confidence=0.9, occurred_at=ts, role="subject")
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT occurred_at, role FROM entity_candidates WHERE normalized_name = %s",
            ("vandelay kickoff sync",)).fetchone()
    assert row["occurred_at"] == ts
    assert row["role"] == "subject"


def test_upsert_entity_class_stores_kind(kb):
    kb.upsert_entity_class("company", active=False)           # seeded kind
    kb.upsert_entity_class("weird gadget", active=False, kind="system")
    with kb._pool.connection() as conn:
        kinds = {r["canonical_name"]: r["kind"] for r in conn.execute(
            "SELECT canonical_name, kind FROM entities WHERE entity_type = 'class'").fetchall()}
    assert kinds["company"] == "organization"
    assert kinds["weird gadget"] == "system"


def test_resolve_class_name_folds_typos(kb):
    kb.upsert_entity_class("company", active=True)
    assert kb.resolve_class_name("conpany") == "company"
    assert kb.resolve_class_name("icompany") == "company"
    assert kb.resolve_class_name("platform") == "platform"    # distinct name untouched
