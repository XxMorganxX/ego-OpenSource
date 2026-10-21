# tests/test_entity_resolve.py
from kb_core.entity_resolve import run_once

class FullStub:
    def __init__(self, replies):
        self._r = list(replies); self.embed_calls = 0
    def embed(self, texts):
        self.embed_calls += 1
        # 'acme' family near each other; everything else far. Crude deterministic map.
        return [[1.0, 0.0] + [0.0] * 1022 if "Acme" in t else [0.0, 1.0] + [0.0] * 1022
                for t in texts]
    def complete(self, system, user, *, schema=None):
        reply = self._r.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

def test_run_once_describes_reembeds_scans_judges_and_stages(kb):
    kb.upsert_entity("Acme"); kb.upsert_entity("Acme University")
    # describe runs first (one complete per blank entity), then the merge judge
    client = FullStub(['{"description": "Acme the university"}',
                       '{"description": "Acme the university"}',
                       '{"same": true, "reason": "same uni"}'])
    res = run_once(kb, client, max_distance=0.2, top_k=5, batch=32)
    assert res["described"] == 2
    assert res["reembedded"] >= 2
    assert res["staged"] == 1
    assert len(kb.list_merge_suggestions()) == 1

def test_run_once_describes_before_reembed(kb):
    """A freshly described entity is embedded the same pass — describe precedes re-embed."""
    e = kb.upsert_entity("Acme")
    client = FullStub(['{"description": "Acme the university"}'])
    run_once(kb, client, max_distance=0.2, top_k=5, batch=32)
    assert kb.get_entity(e.node.id).description == "Acme the university"
    assert not kb.is_embed_dirty(e.node.id)

def test_run_once_routes_each_role_to_its_own_client():
    calls = {}

    class KB:
        def describe_missing_entities(self, client, limit):
            calls["describe"] = client
            return 0
        def reembed_dirty_entities(self, client, batch):
            calls["reembed"] = client
            return 0
        def find_merge_candidates(self, max_distance, top_k):
            return []
        def judge_and_stage_candidates(self, client, candidates):
            calls["judge"] = client
            return {"staged": 0, "blocked": 0}

    d, j, e = object(), object(), object()
    run_once(KB(), describe_client=d, judge_client=j, embed_client=e)
    assert calls == {"describe": d, "reembed": e, "judge": j}


# ---- provider outages: gated per role; the local steps always run --------------------

class _Remote:
    """A remote-provider completion client (describe or judge) for the outage tests."""
    def __init__(self, provider="claude-cli", replies=(), outage=False):
        self.provider = provider
        self._r = list(replies)
        self._outage = outage
        self.calls = 0

    def complete(self, system, user, *, schema=None):
        from kb_core.ingest.inference import ProviderUnavailable
        self.calls += 1
        if self._outage:
            raise ProviderUnavailable("session limit", provider=self.provider)
        return self._r.pop(0)


def _pause(kb, provider):
    from datetime import datetime, timedelta, timezone
    kb.pause_provider(provider, until=datetime.now(timezone.utc) + timedelta(hours=3),
                      reason="session limit")


def _acme_pair(kb):
    kb.upsert_entity("Acme"); kb.upsert_entity("Acme University")


def test_a_paused_describe_provider_still_reembeds_and_stages(kb):
    """Shipped routing: describe -> claude-cli, judge + embed -> local. A claude-cli pause
    must cost only the descriptions, never the embeddings or the merge suggestions."""
    _acme_pair(kb)
    _pause(kb, "claude-cli")
    describe = _Remote()
    local = FullStub(['{"same": true, "reason": "same uni"}'])
    res = run_once(kb, describe_client=describe, judge_client=local, embed_client=local,
                   max_distance=0.2, top_k=5, batch=32)
    assert describe.calls == 0
    assert res["described"] == 0 and res["reembedded"] >= 2
    assert res["staged"] == 1 and len(kb.list_merge_suggestions()) == 1
    assert res["paused"] == {"describe": "claude-cli"}


def test_a_describe_outage_pauses_its_provider_and_the_local_steps_carry_on(kb):
    _acme_pair(kb)
    describe = _Remote(outage=True)
    local = FullStub(['{"same": true, "reason": "same uni"}'])
    res = run_once(kb, describe_client=describe, judge_client=local, embed_client=local,
                   max_distance=0.2, top_k=5, batch=32)
    assert describe.calls == 1                         # the sweep stopped at the outage
    assert kb.provider_paused("claude-cli") is not None
    assert res["reembedded"] >= 2 and res["staged"] == 1
    assert res["paused"] == {"describe": "claude-cli"}


def test_a_paused_judge_provider_finds_candidates_but_judges_none(kb):
    _acme_pair(kb)
    _pause(kb, "agent")
    judge = _Remote(provider="agent")
    local = FullStub(['{"description": "Acme the university"}',
                      '{"description": "Acme the university"}'])
    res = run_once(kb, describe_client=local, judge_client=judge, embed_client=local,
                   max_distance=0.2, top_k=5, batch=32)
    assert judge.calls == 0
    assert res["candidates"] >= 1 and res["staged"] == 0 and res["blocked"] == 0
    assert kb.list_merge_suggestions() == []
    assert res["paused"] == {"judge": "agent"}


def test_a_judge_outage_pauses_its_provider_and_stages_nothing(kb):
    _acme_pair(kb)
    judge = _Remote(provider="agent", outage=True)
    local = FullStub(['{"description": "Acme the university"}',
                      '{"description": "Acme the university"}'])
    res = run_once(kb, describe_client=local, judge_client=judge, embed_client=local,
                   max_distance=0.2, top_k=5, batch=32)
    assert judge.calls == 1
    assert kb.provider_paused("agent") is not None
    assert res["candidates"] >= 1 and res["staged"] == 0
    assert kb.list_merge_suggestions() == []
    assert res["paused"] == {"judge": "agent"}
