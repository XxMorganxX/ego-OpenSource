"""Tests for the background describe step: describe module + KnowledgeBase methods."""
import json

from kb_core.describe import DESCRIBE_SCHEMA, build_describe_prompt, parse_describe
from kb_core.entities import MAX_CLASS_DESCRIPTION_LEN


class ScriptedDescriber:
    def __init__(self, replies): self._r = list(replies); self.calls = 0
    def embed(self, texts): raise AssertionError("not used")
    def complete(self, system, user, *, schema=None):
        self.calls += 1
        assert schema == DESCRIBE_SCHEMA
        reply = self._r.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


# ---- pure module ----------------------------------------------------------

def test_parse_describe_reads_description():
    assert parse_describe('{"description": " a private research university "}') == "a private research university"


def test_parse_describe_tolerates_garbled_replies():
    assert parse_describe("not json at all") is None
    assert parse_describe('{"description": ""}') is None
    assert parse_describe('["wrong", "shape"]') is None
    assert parse_describe(None) is None


def test_build_describe_prompt_branches_on_kind_and_grounds_in_mentions():
    view = {"name": "Acme", "aliases": ["Acme U"],
            "mentions": ["Alumni notes: Acme reunion in June"]}
    sys_i, usr_i = build_describe_prompt(view, "instance")
    sys_c, usr_c = build_describe_prompt(view, "class")
    assert sys_i != sys_c
    assert "named" in sys_i and "category" in sys_c
    for usr in (usr_i, usr_c):
        assert "Acme" in usr and "Acme U" in usr
        assert "Acme reunion in June" in usr


def test_describe_prompt_supports_concepts_and_dictionary_class_definitions():
    view = {"name": "reward hacking", "aliases": [],
            "mentions": ["Evaluation notes: avoid reward hacking by the judge"]}
    sys_i, _ = build_describe_prompt(view, "instance")
    sys_c, _ = build_describe_prompt(view, "class")

    assert "semantic retrieval anchor" in sys_i.lower()
    assert "concept" in sys_i.lower()
    assert "dictionary definition" in sys_c.lower()
    assert "not a summary" in sys_c.lower()


# ---- selection ------------------------------------------------------------

def test_missing_selection_blank_active_only(kb):
    blank = kb.upsert_entity("Acme")                                  # instance, no description
    kb.upsert_entity("Springfield", description="a town in the Midwest")         # described → excluded
    active = kb.upsert_entity_class("university", active=True)           # active class, blank
    kb.upsert_entity_class("hamlet", active=False)                       # inactive → excluded

    rows = kb.entities_missing_description(limit=10)
    got = {r["node_id"]: r["kind"] for r in rows}
    assert got == {blank.node.id: "instance", active.node.id: "class"}

    assert len(kb.entities_missing_description(limit=1)) == 1


def test_missing_selection_skips_locked(kb):
    e = kb.upsert_entity("Acme")
    with kb._pool.connection() as conn:
        conn.execute("UPDATE entities SET description_locked = true WHERE node_id = %s",
                     (e.node.id,))
    assert kb.entities_missing_description(limit=10) == []


# ---- persist --------------------------------------------------------------

def test_set_entity_description_updates_row_node_and_dirty_flag(kb):
    e = kb.upsert_entity("Acme")
    kb.clear_embed_dirty(e.node.id, vector=[0.0] * 1024, text_hash="x")
    kb.set_entity_description(e.node.id, "a private research university")
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT e.description, e.embed_dirty, e.description_locked, n.content "
            "FROM entities e JOIN nodes n ON n.id = e.node_id WHERE e.node_id = %s",
            (e.node.id,),
        ).fetchone()
    assert row["description"] == "a private research university"
    assert row["content"] == "a private research university"
    assert row["embed_dirty"] is True
    assert row["description_locked"] is False


# ---- the loop -------------------------------------------------------------

def test_describe_missing_entities_persists_good_draft(kb):
    e = kb.upsert_entity("Acme")
    client = ScriptedDescriber(['{"description": "a private research university"}'])
    assert kb.describe_missing_entities(client, limit=10) == 1
    assert kb.get_entity(e.node.id).description == "a private research university"
    assert kb.entities_missing_description(limit=10) == []


def test_degenerate_draft_leaves_entity_blank_for_retry(kb):
    e = kb.upsert_entity("Acme")
    runaway = json.dumps({"description": "a" * (MAX_CLASS_DESCRIPTION_LEN + 1)})
    client = ScriptedDescriber([runaway])
    assert kb.describe_missing_entities(client, limit=10) == 0
    assert kb.get_entity(e.node.id).description is None
    assert len(kb.entities_missing_description(limit=10)) == 1     # re-selected next tick


def test_one_raising_entity_does_not_abort_the_sweep(kb):
    kb.upsert_entity("Acme"); kb.upsert_entity("Springfield")
    client = ScriptedDescriber([RuntimeError("model down"),
                                '{"description": "a town on a river"}'])
    assert kb.describe_missing_entities(client, limit=10) == 1
    assert client.calls == 2


def test_a_provider_outage_aborts_the_sweep_instead_of_skipping_the_entity(kb):
    """A per-entity error is skipped, but a dead provider is not a bad entity: the sweep
    stops at once (one timeout, not one per entity) and the error reaches the caller,
    which pauses the provider. Nothing is written."""
    import pytest

    from kb_core.ingest.inference import ProviderUnavailable

    kb.upsert_entity("Acme"); kb.upsert_entity("Springfield")
    client = ScriptedDescriber([ProviderUnavailable("no agent answered", provider="agent"),
                                '{"description": "a town on a river"}'])
    with pytest.raises(ProviderUnavailable):
        kb.describe_missing_entities(client, limit=10)
    assert client.calls == 1
    assert len(kb.entities_missing_description(limit=10)) == 2
