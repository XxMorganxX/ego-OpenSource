"""EmbeddingEnricher: summary-or-content, re-read, hash-skip, re-embed on change."""

from kb_core.domain import NodePatch, NodeRecord
from kb_core.identity import content_hash
from kb_core.ingest.embedding import EmbeddingEnricher, build_embedding_enricher
from kb_core.ingest.enrichment import Enricher


class FakeEmbeddingClient:
    def __init__(self, dim=1024):
        self.calls = []
        self._dim = dim

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.0] * self._dim for _ in texts]


def _node(kb, **over):
    base = dict(source="agent", external_id="n1", type="note",
                content="hello world", slug="n1", title="T")
    base.update(over)
    return kb.upsert(NodeRecord(**base))


def test_satisfies_enricher_protocol():
    assert isinstance(EmbeddingEnricher(FakeEmbeddingClient()), Enricher)


def test_first_run_embeds_title_plus_content_and_stores_hash(kb):
    node = _node(kb)
    client = FakeEmbeddingClient()
    EmbeddingEnricher(client).enrich(node, kb)
    assert client.calls == [["T\nhello world"]]
    assert kb.get(node.id).embedded_hash == content_hash("T", "hello world")


def test_prefers_summary_and_re_reads_node(kb):
    node = _node(kb)
    kb.update(node.id, NodePatch(summary="a tidy summary"))
    client = FakeEmbeddingClient()
    # Pass the *stale* in-memory node (summary still None); enricher must re-read from the DB.
    EmbeddingEnricher(client).enrich(node, kb)
    assert client.calls == [["T\na tidy summary"]]


def test_skips_when_embed_text_unchanged(kb):
    node = _node(kb)
    client = FakeEmbeddingClient()
    EmbeddingEnricher(client).enrich(node, kb)
    EmbeddingEnricher(client).enrich(kb.get(node.id), kb)
    assert len(client.calls) == 1


def test_reembeds_when_summary_changes(kb):
    node = _node(kb)
    client = FakeEmbeddingClient()
    enr = EmbeddingEnricher(client)
    enr.enrich(node, kb)
    kb.update(node.id, NodePatch(summary="now there is a summary"))
    enr.enrich(kb.get(node.id), kb)
    assert client.calls == [["T\nhello world"], ["T\nnow there is a summary"]]


def test_build_embedding_enricher_default_on():
    assert isinstance(build_embedding_enricher({}), EmbeddingEnricher)


def test_build_embedding_enricher_disabled():
    assert build_embedding_enricher({"KB_EMBED_ENABLED": "0"}) is None


def test_embeds_content_when_no_title(kb):
    node = _node(kb, title=None)
    client = FakeEmbeddingClient()
    EmbeddingEnricher(client).enrich(node, kb)
    assert client.calls == [["hello world"]]            # no title prefix, no "None"
    assert kb.get(node.id).embedded_hash == content_hash(None, "hello world")
