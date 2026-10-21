"""KnowledgeBase.background_reservoir_ids: the reservoir a confidence background is scored on."""
from kb_core.domain import NodeRecord


def _note(kb, ext, vec=None, dead=False):
    n = kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                             content=f"note {ext}", slug=ext))
    if vec is not None:
        kb.set_embedding(n.id, vec, embedded_hash=f"h-{ext}")
    if dead:
        with kb._pool.connection() as c:
            c.execute("UPDATE nodes SET dead_at = now() WHERE id = %s", (n.id,))
    return n


def _unit(i):
    v = [0.0] * 1024
    v[i] = 1.0
    return v


def test_reservoir_samples_only_live_embedded_notes_deterministically(kb):
    live = [_note(kb, f"l{i}", _unit(i)) for i in range(6)]
    _note(kb, "unembedded")
    _note(kb, "dead", _unit(9), dead=True)
    a = kb.background_reservoir_ids(4, seed="s1")
    assert len(a) == 4 and set(a) <= {n.id for n in live}
    assert kb.background_reservoir_ids(4, seed="s1") == a          # same seed, same sample
    assert set(kb.background_reservoir_ids(100, seed="s2")) == {n.id for n in live}
