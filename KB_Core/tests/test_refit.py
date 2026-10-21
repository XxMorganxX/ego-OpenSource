from kb_core.domain import NodeRecord
from kb_core.entities import UNTYPED_CATEGORY
from kb_core.ingest.llm import LLMEnricher, LLMPass
from kb_core.ingest.promotion import EntityPromotionWorker


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


class _RecordingClient:
    def __init__(self, reply): self.calls = []; self._reply = reply
    def complete(self, system, user, *, schema=None):
        self.calls.append((system, user, schema)); return self._reply


def _cand_rows(kb, memory_id):
    with kb._pool.connection() as conn:
        return [(r["entity_type"], r["promoted"]) for r in conn.execute(
            "SELECT entity_type, promoted FROM entity_candidates "
            "WHERE memory_id = %s AND entity_type <> 'class' ORDER BY entity_type",
            (memory_id,)).fetchall()]


def _extract(kb, node, entities):
    import json
    passes = [LLMPass("extract_entities", "sys", "{content}", target="entities",
                      schema={"type": "object", "required": ["entities"]})]
    LLMEnricher(_RecordingClient(json.dumps({"entities": entities})), passes).enrich(node, kb)


def _is_a(kb, name):
    inst = next(e for e in kb.list_entities(entity_type="instance") if e.canonical_name == name)
    return sorted(c.canonical_name for c in kb.classes_of(inst.node.id))


def _mentions(kb, name):
    inst = next(e for e in kb.list_entities(entity_type="instance") if e.canonical_name == name)
    return len(kb.memories_linked(inst.node.id, link_type="mentions"))


def test_memory_instance_summary_lists_votes_or_none(kb):
    m = kb.upsert(_mem("e0"))
    assert kb.memory_instance_summary(m.id) == "(none)"
    kb.stage_entity_candidate(m.id, "organization", "Vandelay")
    kb.stage_entity_candidate(m.id, UNTYPED_CATEGORY, "Mystery")
    out = kb.memory_instance_summary(m.id)
    assert "- Vandelay — organization" in out
    assert "- Mystery — untyped" in out


def test_memory_instance_summary_includes_aliases(kb):
    # After a merge, the absorbed name becomes an alias of the survivor.
    # memory_instance_summary must surface that alias so the extract prompt can
    # instruct the model to reuse the canonical name (not re-mint the old one).
    kb.upsert_entity_class("organization", active=True)
    m0 = kb.upsert(_mem("ia0"))
    m1 = kb.upsert(_mem("ia1"))
    m2 = kb.upsert(_mem("ia2"))
    m3 = kb.upsert(_mem("ia3"))
    # Promote both "Vandelay" and "Vandelay Inc" as separate instances
    for m in (m0, m1):
        kb.stage_entity_candidate(m.id, "organization", "Vandelay")
    for m in (m2, m3):
        kb.stage_entity_candidate(m.id, "organization", "Vandelay Inc")
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()
    # Get the two instances
    from kb_core.entities import ENTITY_INSTANCE
    instances = {e.canonical_name: e for e in kb.list_entities(entity_type=ENTITY_INSTANCE)}
    src = instances["Vandelay Inc"]
    tgt = instances["Vandelay"]
    kb.merge_instances(src.node.id, tgt.node.id)
    # m0 votes for "Vandelay"; the merged-away name "Vandelay Inc" is now an alias
    out = kb.memory_instance_summary(m0.id)
    assert "Vandelay Inc" in out, (
        f"alias 'Vandelay Inc' not surfaced in instance summary: {out!r}"
    )


def test_reconcile_keeps_human_is_a_drops_unsupported_derived(kb):
    # A human-origin is_a survives reconcile with no vote support; an unsupported derived one is dropped.
    kb.upsert_entity_class("project", active=True)
    kb.upsert_entity_class("organization", active=True)
    inst = kb.upsert_entity("Vandelay")                 # instance node, no class links yet
    proj = kb.get_entity_class("project")
    org = kb.get_entity_class("organization")
    with kb._pool.connection() as conn:
        conn.execute("INSERT INTO entity_class_links (instance_id, class_id, origin) "
                     "VALUES (%s, %s, 'human')", (inst.node.id, proj.node.id))
        conn.execute("INSERT INTO entity_class_links (instance_id, class_id, origin) "
                     "VALUES (%s, %s, 'derived')", (inst.node.id, org.node.id))
    kb.reconcile_instance_links()
    assert _is_a(kb, "Vandelay") == ["project"]         # human kept, unsupported derived retracted


def test_deactivation_alone_keeps_is_a(kb):
    # Deactivation is class dormancy, not un-typing: an instance stays is_a a class that still
    # has candidate support, even if the class is no longer active. (Retraction happens only on
    # block, or when the supporting votes disappear — see the re-type/drop tests.)
    kb.upsert_entity_class("fad", active=True)
    m0, m1 = kb.upsert(_mem("a4")), kb.upsert(_mem("a5"))
    for m in (m0, m1):
        _extract(kb, m, [{"types": ["fad"], "name": "Widget", "confidence": 0.9}])
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()
    assert _is_a(kb, "Widget") == ["fad"]
    kb.deactivate_class("fad")
    kb.reconcile_instance_links()
    assert _is_a(kb, "Widget") == ["fad"]      # still typed: support (candidates) persists


def test_refit_aggregate_retracts_stale_type(kb):
    kb.upsert_entity_class("organization", active=True)
    kb.upsert_entity_class("project", active=True)
    m0, m1 = kb.upsert(_mem("a0")), kb.upsert(_mem("a1"))
    for m in (m0, m1):
        _extract(kb, m, [{"types": ["organization"], "name": "Globex", "confidence": 0.9}])
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()
    assert _is_a(kb, "Globex") == ["organization"]
    for m in (m0, m1):                                  # both re-type to project
        _extract(kb, m, [{"types": ["project"], "name": "Globex", "confidence": 0.9}])
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()
    assert _is_a(kb, "Globex") == ["project"]          # organization faded (no memory votes it)


def test_refit_retracts_mention_when_memory_drops_instance(kb):
    kb.upsert_entity_class("organization", active=True)
    m0, m1 = kb.upsert(_mem("a2")), kb.upsert(_mem("a3"))
    for m in (m0, m1):
        _extract(kb, m, [{"types": ["organization"], "name": "Acme", "confidence": 0.9}])
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()
    assert _mentions(kb, "Acme") == 2
    _extract(kb, m1, [])                                # m1 no longer mentions Acme
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()
    assert _mentions(kb, "Acme") == 1


def test_extract_prompt_receives_existing_instances(kb):
    node = kb.upsert(_mem("e1"))
    kb.stage_entity_candidate(node.id, "organization", "Vandelay")
    passes = [LLMPass("extract_entities", "sys",
                      "Previously: {existing_instances}\n{content}",
                      target="entities", schema={"type": "object", "required": ["entities"]})]
    client = _RecordingClient('{"entities": []}')
    LLMEnricher(client, passes).enrich(node, kb)
    user_prompt = client.calls[0][1]
    assert "Vandelay" in user_prompt          # the memory's existing instance was injected


def test_unchanged_recrawl_preserves_promoted_vote_no_churn(kb):
    kb.upsert_entity_class("organization", active=True)
    m0, m1 = kb.upsert(_mem("c0")), kb.upsert(_mem("c1"))
    for m in (m0, m1):
        _extract(kb, m, [{"types": ["organization"], "name": "Vandelay", "confidence": 0.9}])
    assert EntityPromotionWorker(kb, threshold=2, class_floor=0).run() >= 1
    assert _cand_rows(kb, m0.id) == [("organization", True)]        # promoted
    # Recrawl m0 with the SAME extraction -> the promoted row is preserved, not churned.
    _extract(kb, m0, [{"types": ["organization"], "name": "Vandelay", "confidence": 0.9}])
    assert _cand_rows(kb, m0.id) == [("organization", True)]        # still exactly one, still promoted


def test_recrawl_retypes_and_drops_only_the_delta(kb):
    kb.upsert_entity_class("organization", active=True)
    kb.upsert_entity_class("project", active=True)
    m = kb.upsert(_mem("c2"))
    _extract(kb, m, [{"types": ["organization"], "name": "Vandelay", "confidence": 0.9},
                     {"types": ["organization"], "name": "Acme", "confidence": 0.9}])
    assert sorted(_cand_rows(kb, m.id)) == [("organization", False), ("organization", False)]
    # Recrawl: Vandelay becomes a project; Acme is dropped.
    _extract(kb, m, [{"types": ["project"], "name": "Vandelay", "confidence": 0.9}])
    types = sorted(t for t, _ in _cand_rows(kb, m.id))
    assert types == ["project"]            # organization(Vandelay) re-typed, Acme removed


def test_prune_memory_instances_empty_keep_deletes_all(kb):
    m = kb.upsert(_mem("c3"))
    kb.stage_entity_candidate(m.id, "organization", "X")
    assert kb.prune_memory_instances(m.id, set()) == 1
    assert _cand_rows(kb, m.id) == []


def test_recrawl_retype_prunes_a_promoted_vote(kb):
    kb.upsert_entity_class("organization", active=True)
    kb.upsert_entity_class("project", active=True)
    m = kb.upsert(_mem("rt0"))
    m2 = kb.upsert(_mem("rt1"))
    for mm in (m, m2):
        _extract(kb, mm, [{"types": ["organization"], "name": "Vandelay", "confidence": 0.9}])
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()
    assert _cand_rows(kb, m.id) == [("organization", True)]            # promoted
    _extract(kb, m, [{"types": ["project"], "name": "Vandelay", "confidence": 0.9}])  # re-type
    assert _cand_rows(kb, m.id) == [("project", False)]                # old PROMOTED vote pruned
