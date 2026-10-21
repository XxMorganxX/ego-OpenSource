from kb_core.denoise_instances import select_noise, apply_noise
from kb_core.domain import NodeRecord
from kb_core.entities import STATUS_INACTIVE
from kb_core.meaning_judge import MEANING_SCHEMA


class ScriptedJudge:
    def __init__(self, verdicts): self.verdicts = dict(verdicts)   # name -> keep bool
    def embed(self, texts): raise AssertionError("not used")
    def complete(self, system, user, *, schema=None):
        assert schema == MEANING_SCHEMA
        # Couples to build_meaning_prompt's first line ("name: <name>"); update if that changes.
        name = user.split("name: ", 1)[1].splitlines()[0]
        keep = self.verdicts.get(name, True)
        return '{"keep": %s, "reason": "judged"}' % ("true" if keep else "false")


def _instance(kb, name, *, pinned=False):
    ent = kb.upsert_entity(name, categories=["tool"])
    if pinned:
        with kb._pool.connection() as conn:
            conn.execute("UPDATE entities SET pinned = true WHERE node_id = %s", (ent.node.id,))
    return ent.node.id


def test_select_flags_name_junk_and_judge_drops_but_spares_pinned(kb):
    _instance(kb, "SKU_FIELD_MAP")                         # name gate -> flagged
    _instance(kb, "build_pick_list")                        # judge -> drop
    _instance(kb, "lantern_sync")                         # judge -> keep
    _instance(kb, "chrome_stuff", pinned=True)              # would be judged, but pinned -> spared
    client = ScriptedJudge({"build_pick_list": False, "lantern_sync": True,
                            "chrome_stuff": False})
    rows = {r["name"]: r for r in select_noise(kb, client)}
    assert set(rows) == {"SKU_FIELD_MAP", "build_pick_list"}
    assert rows["SKU_FIELD_MAP"]["origin"] == "name"
    assert rows["build_pick_list"]["origin"] == "judge"


def test_broken_prompts_overlay_aborts_before_any_instance_is_judged(kb, tmp_path):
    """select_noise resolves the judge's system prompt ONCE, unconditionally, before the
    loop, whenever a client is configured -- a broken overlay (missing include, missing
    file) is a configuration error and must abort the whole backfill up front, not be
    discovered only if/when some row happens to need the judge, and never be swallowed
    by the `except Exception: continue` fail-open path below (that path exists for
    inference errors, not config errors).

    Seeded with ONLY a deterministically-flagged name (the name gate alone catches it,
    so the judge is never actually needed for this data): the old per-row placement
    would never have called `kb.prompt` at all here and would have returned normally --
    only an unconditional up-front resolve catches a broken overlay in this case."""
    import pytest

    from kb_core.knowledge_base import KnowledgeBase
    from kb_core.policy import Policy, PolicyError, Prompts
    from kb_core.registry import default_registry

    class CountingClient:
        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            raise AssertionError("not used")

        def complete(self, system, user, *, schema=None):
            self.calls += 1
            return '{"keep": true, "reason": "judged"}'

    _instance(kb, "SKU_FIELD_MAP")             # name gate alone flags it -> judge not needed
    (tmp_path / "meaning_judge.system.md").write_text("{{include:missing.md}}")
    broken = KnowledgeBase(kb._pool, default_registry(),
                           policy=Policy(prompts=Prompts(str(tmp_path))))
    client = CountingClient()
    with pytest.raises(PolicyError):
        select_noise(broken, client)
    assert client.calls == 0


def test_apply_demotes_and_blocks(kb):
    nid = _instance(kb, "build_pick_list")
    for i in range(2):                                       # give it re-promotable candidates
        m = kb.upsert(NodeRecord(source="agent", external_id=f"m{i}", type="note",
                                 content="build_pick_list", slug=f"m{i}")).id
        kb.stage_entity_candidate(m, "tool", "build_pick_list")
    rows = [{"id": nid, "name": "build_pick_list", "support": 0, "reason": "fn", "origin": "judge"}]
    assert apply_noise(kb, rows) == 1
    with kb._pool.connection() as conn:
        st = conn.execute("SELECT status FROM entities WHERE node_id = %s", (nid,)).fetchone()["status"]
    assert st == STATUS_INACTIVE
    assert all(g["normalized_name"] != "build_pick_list"     # blocked -> can't re-promote
               for g in kb.promotable_entities(threshold=2))
