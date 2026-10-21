from kb_core.domain import NodeRecord
from kb_core.ingest.promotion import EntityPromotionWorker
from kb_core.meaning_judge import MEANING_SCHEMA


class ScriptedJudge:
    """Stub inference client — returns scripted JSON, asserts the meaning schema is used."""
    def __init__(self, reply): self.reply = reply; self.calls = 0
    def embed(self, texts): raise AssertionError("not used")
    def complete(self, system, user, *, schema=None):
        self.calls += 1
        assert schema == MEANING_SCHEMA
        return self.reply


def _stage(kb, eid, category, name):
    m = kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                             content=f"{name} appears here", slug=eid)).id
    kb.stage_entity_candidate(m, category, name)


def test_reject_blocks_and_does_not_promote(kb):
    for i in range(2):
        _stage(kb, f"a{i}", "tool", "build_pick_list")
    client = ScriptedJudge('{"keep": false, "reason": "internal function"}')
    settled = EntityPromotionWorker(kb, threshold=2, inference_client=client).run()
    assert client.calls == 1
    assert kb.instances_of("tool") == []                       # not promoted
    assert kb.promotable_entities(threshold=2) == []           # blocked -> excluded


def test_keep_promotes(kb):
    for i in range(2):
        _stage(kb, f"b{i}", "tool", "lantern_sync")
    client = ScriptedJudge('{"keep": true, "reason": "named toolset"}')
    EntityPromotionWorker(kb, threshold=2, inference_client=client).run()
    assert [e.canonical_name for e in kb.instances_of("tool")] == ["lantern_sync"]


def test_no_client_promotes_as_before(kb):
    for i in range(2):
        _stage(kb, f"c{i}", "tool", "build_pick_list")
    EntityPromotionWorker(kb, threshold=2).run()               # no client
    assert [e.canonical_name for e in kb.instances_of("tool")] == ["build_pick_list"]


def test_broken_prompts_overlay_raises_at_construction_when_a_client_is_configured(kb, tmp_path):
    """The meaning judge's system prompt is resolved ONCE at worker construction, not per
    candidate -- a broken overlay (missing include, missing file) is a configuration
    error and must fail loud when the worker is built (so the ingest daemon fails at
    start), never be swallowed candidate-by-candidate by `_is_meaningful`'s fail-open
    try/except (that except exists for inference errors, not config errors)."""
    import pytest

    from kb_core.knowledge_base import KnowledgeBase
    from kb_core.policy import Policy, PolicyError, Prompts
    from kb_core.registry import default_registry

    (tmp_path / "meaning_judge.system.md").write_text("{{include:missing.md}}")
    broken = KnowledgeBase(kb._pool, default_registry(),
                           policy=Policy(prompts=Prompts(str(tmp_path))))
    client = ScriptedJudge('{"keep": true, "reason": "x"}')
    with pytest.raises(PolicyError):
        EntityPromotionWorker(broken, threshold=2, inference_client=client)
    assert client.calls == 0


def test_broken_prompts_overlay_does_not_raise_when_no_client_is_configured(kb, tmp_path):
    """No inference client means the meaning gate is off -- the judge's system prompt is
    never resolved, so a broken overlay must NOT block constructing a gate-off worker."""
    from kb_core.knowledge_base import KnowledgeBase
    from kb_core.policy import Policy, Prompts
    from kb_core.registry import default_registry

    (tmp_path / "meaning_judge.system.md").write_text("{{include:missing.md}}")
    broken = KnowledgeBase(kb._pool, default_registry(),
                           policy=Policy(prompts=Prompts(str(tmp_path))))
    EntityPromotionWorker(broken, threshold=2)             # no client -> no raise


def test_block_holds_across_sibling_category_in_same_sweep(kb):
    # Same name staged under two categories -> two promotable groups in one sweep. A reject on
    # the first must not be re-judged (or promoted) via the second.
    for i in range(2):
        _stage(kb, f"d{i}", "tool", "build_pick_list")
    for i in range(2):
        _stage(kb, f"e{i}", "system component", "build_pick_list")
    client = ScriptedJudge('{"keep": false, "reason": "internal function"}')
    EntityPromotionWorker(kb, threshold=2, inference_client=client).run()
    assert client.calls == 1                                    # judged once, not per category
    assert kb.instances_of("tool") == []
    assert kb.instances_of("system component") == []


class OfflineJudge:
    """The tenant's kb-agent is offline: every judge call is a provider outage."""
    provider = "agent"

    def __init__(self): self.calls = 0

    def complete(self, system, user, *, schema=None):
        from datetime import datetime, timedelta, timezone

        from kb_core.ingest.inference import ProviderUnavailable
        self.calls += 1
        raise ProviderUnavailable("no agent answered", provider="agent",
                                  until=datetime.now(timezone.utc) + timedelta(minutes=10))


def test_a_provider_outage_is_neither_a_keep_nor_a_block(kb):
    """Fail-open covers a bad reply, not a dead provider: an outage must not promote an
    unjudged name. The sweep stops judging after the first outage (no N x timeout wait),
    pauses the provider, and leaves every candidate promotable for the next tick."""
    for name, pre in (("build_pick_list", "f"), ("lantern_sync", "g")):
        for i in range(2):
            _stage(kb, f"{pre}{i}", "tool", name)
    client = OfflineJudge()
    EntityPromotionWorker(kb, threshold=2, inference_client=client).run()
    assert client.calls == 1                                   # stopped after the outage
    assert kb.instances_of("tool") == []                       # nothing promoted unjudged
    assert len(kb.promotable_entities(threshold=2)) == 2       # nothing blocked either
    assert kb.provider_paused("agent") is not None


def test_a_paused_provider_is_not_called_at_all(kb):
    from datetime import datetime, timedelta, timezone

    for i in range(2):
        _stage(kb, f"h{i}", "tool", "build_pick_list")
    kb.pause_provider("agent", until=datetime.now(timezone.utc) + timedelta(minutes=10),
                      reason="offline")
    client = OfflineJudge()
    EntityPromotionWorker(kb, threshold=2, inference_client=client).run()
    assert client.calls == 0
    assert kb.instances_of("tool") == []
    assert len(kb.promotable_entities(threshold=2)) == 1
