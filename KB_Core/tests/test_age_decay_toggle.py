"""The content-age decay master switch (age_decay_state). The flag
gates all three analysis sites through aging.age_params: OFF rides age_floor=1.0,
which collapses the SQL factor to exactly 1.0 for every row — no SQL branching."""
from datetime import datetime, timedelta, timezone

from kb_core.aging import age_params
from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS, normalize_entity_name
from tests.legacy_policy import LEGACY_EMAIL_RULE


def _email_at(eid, days_ago):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c",
                      slug=eid, title=eid, tags=["email"],
                      event_at=datetime.now(timezone.utc) - timedelta(days=days_ago))


def _vec(axis):
    v = [0.0] * 1024
    v[axis] = 1.0
    return v


def test_age_params_off_rides_every_floor_as_one():
    on = age_params((LEGACY_EMAIL_RULE,), True)
    off = age_params((LEGACY_EMAIL_RULE,), False)
    assert on["d0_floor"] == LEGACY_EMAIL_RULE.floor
    assert off["d0_floor"] == 1.0
    assert on["d0_hl_secs"] == off["d0_hl_secs"] == 17 * 86400.0


def test_default_enabled_and_flip_roundtrip(kb):
    assert kb.age_decay_enabled() is True                # seeded by 0044
    row = kb.set_age_decay(False, updated_by="test")
    assert row["enabled"] is False and row["updated_by"] == "test"
    assert kb.age_decay_enabled() is False
    kb.set_age_decay(True)
    assert kb.age_decay_enabled() is True


def test_knn_weights_undecayed_when_off(kb):
    old1, old2 = kb.upsert(_email_at("o1", 365)), kb.upsert(_email_at("o2", 365))
    kb.set_embedding(old1.id, _vec(0), embedded_hash="h1")
    kb.set_embedding(old2.id, _vec(0), embedded_hash="h2")
    assert all(float(r["weight"]) < 0.3                  # ON (default): near floor
               for r in kb.memory_knn_edges(k=4, min_sim=0.5))
    kb.set_age_decay(False)
    assert all(float(r["weight"]) > 0.99                 # OFF: weight == sim
               for r in kb.memory_knn_edges(k=4, min_sim=0.5))


def test_mention_age_factor_one_when_off(kb):
    m = kb.upsert(_email_at("m1", 365))
    ent = kb.upsert_entity("Ollama", categories=["tool"])
    kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)
    assert float(kb.concept_mention_edges()[0]["age_factor"]) < 0.3
    kb.set_age_decay(False)
    assert float(kb.concept_mention_edges()[0]["age_factor"]) == 1.0


def test_dormant_emails_promote_when_off(kb):
    for i in range(3):
        m = kb.upsert(_email_at(f"e{i}", 365))
        kb.stage_entity_candidate(m.id, "people", "Avery")
    assert kb.promotable_entities(threshold=3) == []     # ON: held back
    kb.set_age_decay(False)
    names = {g["normalized_name"] for g in kb.promotable_entities(threshold=3)}
    assert names == {normalize_entity_name("Avery")}
