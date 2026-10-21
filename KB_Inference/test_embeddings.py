"""Unit tests for the embedder's context knob (no Ollama contact)."""

import embeddings
from embeddings import EmbeddingService


class _FakeOllamaEmbeddings:
    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeOllamaEmbeddings.last_kwargs = kwargs


def test_num_ctx_is_passed_to_the_embedder(monkeypatch):
    monkeypatch.setattr(embeddings, "OllamaEmbeddings", _FakeOllamaEmbeddings)
    EmbeddingService(num_ctx=2048)
    assert _FakeOllamaEmbeddings.last_kwargs["num_ctx"] == 2048


def test_none_num_ctx_leaves_the_model_default(monkeypatch):
    monkeypatch.setattr(embeddings, "OllamaEmbeddings", _FakeOllamaEmbeddings)
    EmbeddingService(num_ctx=None)
    assert "num_ctx" not in _FakeOllamaEmbeddings.last_kwargs


def test_default_ctx_covers_the_char_clip():
    # The clip is 6,000 chars (~1,500 tokens); the window must exceed it or we'd
    # silently truncate inputs the clip deliberately let through.
    assert embeddings.DEFAULT_EMBED_NUM_CTX == 2048
    assert embeddings.DEFAULT_EMBED_MAX_CHARS / 4 < embeddings.DEFAULT_EMBED_NUM_CTX
