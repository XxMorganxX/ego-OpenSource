from kb_core.domain import NodeRecord
from kb_core.identity import content_hash
from kb_core.ingest.embedding import (
    CHUNK_MAX, CHUNK_MIN_TEXT, EmbeddingEnricher, _chunk_spans,
)


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _blend(hot, other, sim):
    """Unit vector with cosine exactly `sim` to _vec(hot) — a genuine-but-weaker
    neighbour, which orthogonal _vec pairs cannot express now that the kNN arm
    floors at KNN_MIN_SIM."""
    v = [0.0] * 1024
    v[hot] = sim
    v[other] = (1 - sim * sim) ** 0.5
    return v


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


class _StubClient:
    def __init__(self, vec):
        self.vec = vec
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [self.vec] * len(texts)


def test_chunk_spans_window_overlap_and_caps():
    assert _chunk_spans("x" * CHUNK_MIN_TEXT) == []          # short: no chunks
    spans = _chunk_spans("x" * 5000)
    assert spans[0] == (0, 1500) and spans[1] == (1200, 2700)  # 300-char overlap
    assert spans[-1][1] == 5000                               # tail covered
    assert len(_chunk_spans("x" * 100_000)) == CHUNK_MAX      # runaway bound


def test_search_scores_note_by_best_chunk(kb):
    long_note = _note(kb, "long", "head is about something else entirely " * 60)
    short = _note(kb, "short", "unrelated filler")
    kb.set_embedding(long_note.id, _vec(0), embedded_hash="h1")  # head vector: far
    kb.set_embedding(short.id, _vec(2), embedded_hash="h2")
    kb.set_chunk_embeddings(long_note.id, [(2000, 3500, _vec(1))])  # tail: exact
    res = kb.search("no lexical overlap words", embedding=_vec(1))
    assert res[0].node.external_id == "long"
    assert res[0].similarity > 0.99          # the chunk's sim, not the head's


def test_facet_filters_bind_chunk_candidates(kb):
    tagged = _note(kb, "tagged_c", "alpha " * 500, tags=["keep"])
    dropped = _note(kb, "dropped_c", "beta " * 500, tags=["drop"])
    # `dropped_c`'s chunk is the perfect match; `tagged_c`'s is merely good. The tags
    # filter, not the similarity floor, must be what excludes the better one.
    for n, chunk in ((tagged, _blend(4, 3, 0.8)), (dropped, _vec(4))):
        kb.set_embedding(n.id, _vec(9), embedded_hash="h")
        kb.set_chunk_embeddings(n.id, [(0, 1500, chunk)])
    res = kb.search("nothing lexical", embedding=_vec(4), tags=["keep"])
    assert [r.node.external_id for r in res] == ["tagged_c"]


def test_enricher_builds_chunks_for_long_notes_once(kb):
    node = _note(kb, "enr_long", "body words " * 400)        # ~4400 chars
    client = _StubClient(_vec(5))
    enricher = EmbeddingEnricher(client)
    enricher.enrich(node, kb)
    assert kb.chunk_count(node.id) >= 3
    calls = client.calls
    enricher.enrich(kb.get(node.id), kb)                     # unchanged -> no-op
    assert client.calls == calls


def test_enricher_backfills_chunks_when_hash_already_current(kb):
    """A note embedded BEFORE chunking existed: hash matches, chunks missing —
    the enricher must still build them (the live-backfill path)."""
    node = _note(kb, "backfill", "tail matter " * 400)
    h = content_hash(node.title, node.content)
    kb.set_embedding(node.id, _vec(6), embedded_hash=h)      # pre-chunking state
    EmbeddingEnricher(_StubClient(_vec(6))).enrich(kb.get(node.id), kb)
    assert kb.chunk_count(node.id) >= 3


def test_enricher_leaves_short_notes_chunkless(kb):
    node = _note(kb, "enr_short", "brief")
    EmbeddingEnricher(_StubClient(_vec(7))).enrich(node, kb)
    assert kb.chunk_count(node.id) == 0


class _IdentifiedStub(_StubClient):
    """Embed client that declares an identity, like HTTPInferenceClient does."""
    provider = "local"

    def embed(self, texts):
        self.embed_model = "qwen3-embedding:0.6b"   # stashed by the call, as the real client does
        return super().embed(texts)


def test_embed_enrich_reports_the_served_model(kb):
    node = _note(kb, "prov-emb", "some content")
    report = EmbeddingEnricher(_IdentifiedStub(_vec(0))).enrich(node, kb)
    assert report == {"providers": ["local:qwen3-embedding:0.6b"]}


def test_embed_enrich_skip_path_reports_none(kb):
    node = _note(kb, "prov-skip", "some content")
    client = _IdentifiedStub(_vec(0))
    EmbeddingEnricher(client).enrich(node, kb)               # embeds, stores hash
    node = kb.get(node.id)
    calls_before = client.calls
    report = EmbeddingEnricher(client).enrich(node, kb)      # unchanged -> skip
    assert report is None
    assert client.calls == calls_before                       # no model call happened


def test_embed_enrich_with_unidentified_client_reports_none(kb):
    node = _note(kb, "prov-bare", "some content")
    report = EmbeddingEnricher(_StubClient(_vec(0))).enrich(node, kb)
    assert report is None
