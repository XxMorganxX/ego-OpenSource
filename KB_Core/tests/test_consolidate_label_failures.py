"""A failing concept-label client must be surfaced (loud), not swallowed silently,
while consolidation stays fail-open — structure survives, only the name degrades.

If `_label` swallowed errors (`except: pass`), a broken label client (e.g. expired
provider auth) would silently degrade every concept label to a placeholder.

The embedding half gets the same treatment: a consolidation run that loses every concept
embedding (e.g. to an unset KB_INFERENCE_URL) must report why, not just a bare count.
"""
import json

from kb_core.concept_judge import is_fallback_name
from kb_core.consolidate import run_once
from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS


class FailingLabelClient:
    """Every label call fails (mimics the OAuth-expired claude-cli); embeddings still work."""
    def complete(self, system_prompt, user_prompt, *, schema=None):
        raise RuntimeError("claude CLI exited 1: OAuth session expired")

    def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]


def _seed_one_cluster(kb):
    ents = [kb.upsert_entity(e, categories=["tool"]) for e in ("AlphaRepo", "AlphaPerson")]
    for i in range(3):
        m = kb.upsert(NodeRecord(source="agent", external_id=f"a{i}", type="note",
                                 content="note about AlphaRepo", slug=f"a{i}", title=f"a{i}"))
        vec = [0.0] * 1024
        vec[0] = 1.0
        kb.set_embedding(m.id, vec, embedded_hash=f"ha{i}")
        for ent in ents:
            kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)


def test_label_failure_is_surfaced_and_fail_open(kb):
    _seed_one_cluster(kb)
    r = run_once(kb, FailingLabelClient(), min_memories=3)
    # Loud: the failure count and a sample error travel out in the run stats.
    assert r["label_failures"] >= 1
    assert r["label_error"] and "OAuth" in r["label_error"]
    # Fail-open: structure is preserved — the concept is kept, only with a placeholder name.
    assert r["kept"] == 1
    active = kb.active_concepts()
    assert len(active) == 1
    assert is_fallback_name(active[0]["name"])


class FailingEmbeddingClient:
    """Labels fine, embeddings fail — mimics an unset KB_INFERENCE_URL resolving to the
    dashboard port, which answers /embed with a 404."""
    def complete(self, system_prompt, user_prompt, *, schema=None):
        return json.dumps({"coherent": True, "name": "Alpha project",
                           "kind": "project", "description": "seeded cluster"})

    def embed(self, texts):
        raise RuntimeError("HTTP 404 from http://127.0.0.1:8000/embed")


def test_embed_failure_is_surfaced_and_fail_open(kb):
    """A count alone is undiagnosable: embed_failures=N with no reason hides causes such
    as a wrong port. Carry the error out too."""
    _seed_one_cluster(kb)
    r = run_once(kb, FailingEmbeddingClient(), min_memories=3)
    assert r["embed_failures"] == 1
    assert r["embed_error"] and "404" in r["embed_error"]
    # Fail-open: the concept and its LLM label survive; only the vector is missing.
    assert r["kept"] == 1
    assert kb.active_concepts()[0]["name"] == "Alpha project"
