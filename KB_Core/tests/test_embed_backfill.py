"""backfill() embeds every node with embedding IS NULL, in batches, and stops cleanly."""

from kb_core.domain import NodeRecord
from kb_core.embed_backfill import backfill
from kb_core.ingest.embedding import EmbeddingEnricher


class FakeEmbeddingClient:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.0] * 1024 for _ in texts]


def test_backfill_embeds_all_unembedded(kb):
    for i in range(3):
        kb.upsert(NodeRecord(source="agent", external_id=f"b{i}", type="note",
                             content=f"content {i}", slug=f"b{i}", title="T"))
    client = FakeEmbeddingClient()
    n = backfill(kb, EmbeddingEnricher(client), batch=2)
    assert n == 3
    assert kb.unembedded_nodes() == []
    assert len(client.calls) == 3


def test_backfill_skips_already_embedded(kb):
    pre = kb.upsert(NodeRecord(source="agent", external_id="pre", type="note",
                               content="x", slug="pre", title="T"))
    kb.set_embedding(pre.id, [0.0] * 1024, embedded_hash="pre")
    kb.upsert(NodeRecord(source="agent", external_id="new", type="note",
                         content="y", slug="new", title="T"))
    client = FakeEmbeddingClient()
    n = backfill(kb, EmbeddingEnricher(client), batch=10)
    assert n == 1
    assert len(client.calls) == 1
