"""An enricher can be asked to run only some of its configured passes.

Recrawl uses this to run exactly the pass whose dependency moved, rather than all three.
"""

from kb_core.domain import NodeRecord
from kb_core.ingest.llm import LLMEnricher, LLMPass


class _RecordingClient:
    """Records the system prompt of every pass it serves and replies per-pass, so the
    entity_gate pass always comes back eligible (an entity_gate reply is grammar-checked
    by `LLMEnricher._gate`, not by shape alone — a payload shared across all three passes
    would risk the gate reading as ineligible and silently suppressing extract_entities)."""

    _REPLIES = {
        "GATE": '{"noise": false, "stale": false}',
        "EXTRACT": '{"entities": []}',
        "KIND": '{"kind": "note"}',
        "CLASSIFY": '{"classes": []}',
    }

    def __init__(self):
        self.calls = []

    def complete(self, system, user, schema=None):
        self.calls.append(system)
        return self._REPLIES.get(system, '{"kind": "note"}')


def _passes():
    return [
        LLMPass(name="entity_gate", system="GATE", user="{content}",
                target="entity_gate", schema={"type": "object"}, phase="recrawl"),
        LLMPass(name="extract_entities", system="EXTRACT", user="{content}",
                target="entities", schema={"type": "object"}, phase="recrawl"),
        LLMPass(name="doc_kind", system="KIND", user="{content}",
                target="doc_kind", schema={"type": "object"}, phase="recrawl"),
    ]


def _node(kb):
    return kb.upsert(NodeRecord(source="agent", external_id="a", type="note",
                                content="c", slug="a"))


def test_only_runs_the_named_pass(kb):
    client = _RecordingClient()
    node = _node(kb)
    enricher = LLMEnricher(client, _passes())

    enricher.enrich(node, kb, only={"doc_kind"})

    assert client.calls == ["KIND"]


def test_none_runs_every_pass(kb):
    client = _RecordingClient()
    node = _node(kb)
    enricher = LLMEnricher(client, _passes())

    enricher.enrich(node, kb, only=None)

    assert client.calls == ["GATE", "EXTRACT", "KIND"]


def test_subset_keeps_declaration_order(kb):
    client = _RecordingClient()
    node = _node(kb)
    enricher = LLMEnricher(client, _passes())

    enricher.enrich(node, kb, only={"doc_kind", "entity_gate"})

    assert client.calls == ["GATE", "KIND"]      # gate before kind, as declared


def test_no_refresh_call_when_no_entity_pass_runs(kb, monkeypatch):
    """refresh_entity_contribution is only reached via the `p.target in _ENTITY_TARGETS`
    guard inside the pass loop. This pins that guard, not the SOURCE of the classes/
    instances flags passed to it — see test_regen_flags_come_from_the_effective_passes_
    not_all_configured below for that (this fixture has only one entity-target pass, so
    `effective` and `self._passes` can never disagree on the flags here).
    """
    seen = []
    monkeypatch.setattr(type(kb), "refresh_entity_contribution",
                        lambda self, nid, **kw: seen.append(kw))
    node = _node(kb)

    LLMEnricher(_RecordingClient(), _passes()).enrich(node, kb, only={"doc_kind"})

    assert seen == []                            # no entity pass ran -> no refresh


def test_regen_flags_come_from_the_effective_passes_not_all_configured(kb, monkeypatch):
    """Guards the flags' SOURCE, not the loop filter.

    refresh_entity_contribution CLEARS what the run is about to re-stage on the classes
    side (the `instances` parameter is currently a no-op downstream, but the flag is
    still computed and threaded through, so it must stay correct too). If the flags were
    computed from every configured pass instead of the ones actually running, a subset
    run would clear a contribution it will not rebuild. Only reachable when the fixture
    has two entity-target passes, so `effective` and `self._passes` can disagree.
    """
    seen = []
    monkeypatch.setattr(type(kb), "refresh_entity_contribution",
                        lambda self, nid, **kw: seen.append(kw))
    passes = [
        LLMPass(name="classify_memory", system="CLASSIFY", user="{content}",
                target="classify", schema={"type": "object"}, phase="recrawl"),
        LLMPass(name="extract_entities", system="EXTRACT", user="{content}",
                target="entities", schema={"type": "object"}, phase="recrawl"),
    ]
    node = _node(kb)

    LLMEnricher(_RecordingClient(), passes).enrich(node, kb, only={"classify_memory"})

    assert seen == [{"classes": True, "instances": False}]


def test_regen_flags_track_extraction_when_only_it_runs(kb, monkeypatch):
    """The complement of the classify-only case.

    Together these two pin the flags to the effective pass list: one expects
    {classes: True, instances: False}, the other the exact inverse, so no constant pair
    can satisfy both. `refresh_entity_contribution` is still CALLED here — extract_entities
    has target "entities", which is in _ENTITY_TARGETS, so the loop guard is satisfied —
    it just receives classes=False.
    """
    seen = []
    monkeypatch.setattr(type(kb), "refresh_entity_contribution",
                        lambda self, nid, **kw: seen.append(kw))
    passes = [
        LLMPass(name="classify_memory", system="CLASSIFY", user="{content}",
                target="classify", schema={"type": "object"}, phase="recrawl"),
        LLMPass(name="extract_entities", system="EXTRACT", user="{content}",
                target="entities", schema={"type": "object"}, phase="recrawl"),
    ]
    node = _node(kb)

    LLMEnricher(_RecordingClient(), passes).enrich(node, kb, only={"extract_entities"})

    assert seen == [{"classes": False, "instances": True}]
