"""Per-memory entity-contribution refresh: an analysis pass REPLACES a memory's prior
(unpromoted) votes/links of the kind it regenerates, instead of stacking them across
re-analysis. Promoted candidates and mention links (the materialized graph) are never touched."""

from kb_core.domain import NodeRecord
from kb_core.entities import BELONGS_TO, ENTITY_CLASS
from kb_core.ingest.llm import LLMEnricher, LLMPass


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


class FakeClient:
    def __init__(self, replies):
        self._r = list(replies)

    def complete(self, system, user, schema=None):
        return self._r.pop(0)


def _cands(pool, memory_id):
    with pool.connection() as c:
        return {(r["entity_type"], r["normalized_name"], r["promoted"])
                for r in c.execute(
                    "SELECT entity_type, normalized_name, promoted FROM entity_candidates "
                    "WHERE memory_id = %s", (memory_id,)).fetchall()}



def test_refresh_classes_clears_class_votes_and_belongs_to(kb, pool):
    m = kb.upsert(_mem("m1"))
    cls = kb.upsert_entity_class("projects")
    kb.stage_entity_candidate(m.id, ENTITY_CLASS, "projects")     # class vote -> cleared
    kb.stage_entity_candidate(m.id, "people", "Avery")          # instance vote -> kept
    kb.link_entity(m.id, cls.node.id, BELONGS_TO)                # belongs_to -> cleared

    kb.refresh_entity_contribution(m.id, classes=True, instances=False)

    got = _cands(pool, m.id)
    assert ("class", "projects", False) not in got
    assert ("people", "avery", False) in got
    with pool.connection() as c:
        n = c.execute("SELECT count(*) n FROM entity_links WHERE memory_id = %s AND link_type = %s",
                      (m.id, BELONGS_TO)).fetchone()["n"]
    assert n == 0


def test_recrawl_replaces_instance_votes_instead_of_stacking(kb):
    node = kb.upsert(_mem("m1"))
    kb.stage_entity_candidate(node.id, "people", "Drifted")     # a stale vote from a past crawl
    passes = [LLMPass("extract_entities", "s", "u {content}", target="entities", phase="recrawl",
                      schema={"type": "object", "required": ["entities"]})]
    reply = '{"entities":[{"category":"people","name":"Current","confidence":0.9}]}'
    LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    names = {g["normalized_name"] for g in kb.promotable_entities(threshold=1)}
    assert "current" in names and "drifted" not in names         # replaced, not stacked


def test_drain_removes_unpromoted_instances_corpuswide(kb, pool):
    m1, m2 = kb.upsert(_mem("m1")), kb.upsert(_mem("m2"))
    kb.stage_entity_candidate(m1.id, "people", "A")
    kb.stage_entity_candidate(m2.id, "people", "B")
    kb.stage_entity_candidate(m1.id, ENTITY_CLASS, "projects")   # class vote -> kept
    with pool.connection() as c:
        c.execute("UPDATE entity_candidates SET promoted = true WHERE normalized_name = 'a'")

    assert kb.drain_unpromoted_instance_candidates() == 1        # only m2/B
    remaining = _cands(pool, m1.id) | _cands(pool, m2.id)
    assert ("people", "b", False) not in remaining
    assert ("people", "a", True) in remaining                    # promoted kept
    assert ("class", "projects", False) in remaining            # class vote kept
