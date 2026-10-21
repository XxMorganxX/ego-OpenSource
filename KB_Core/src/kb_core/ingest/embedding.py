"""Embedding enrichment: turn each node's text into a vector via KB_Inference /embed.

Implements the Enricher protocol so it slots into EnrichmentWorker / ReanalysisWorker with no
worker changes. It embeds the node's summary (its distilled signal) when present, else the raw
content, prefixed with the title. A content_hash of that exact text (stored as embedded_hash)
lets it skip nodes whose embed-text is unchanged — so it is cheap to run in both the enrich and
recrawl phases, and picks up summaries that drift on re-analysis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kb_core.domain import Node
from kb_core.identity import content_hash
from kb_core.ingest.inference import EmbeddingClient

if TYPE_CHECKING:
    from kb_core.knowledge_base import KnowledgeBase


def _embed_text(title: str | None, base: str) -> str:
    """The text handed to the model: title prefixed to the body when there is a title."""
    return f"{title}\n{base}" if title else base


# Chunking: the embedder clips input (~6000 chars), so a long note's tail never
# reaches the model, and even a fully-embedded long note averages many passages into
# one vector. Notes longer than CHUNK_MIN_TEXT also get overlapping-window chunk
# vectors (search scores a note by its best vector). CHUNK_MAX bounds runaway notes.
CHUNK_SIZE = 1500
CHUNK_STEP = 1200            # 300-char overlap so a passage never straddles a cut
CHUNK_MIN_TEXT = 2000
CHUNK_MAX = 20


def _chunk_spans(text: str) -> "list[tuple[int, int]]":
    if len(text) <= CHUNK_MIN_TEXT:
        return []
    spans = []
    for start in range(0, len(text), CHUNK_STEP):
        spans.append((start, min(start + CHUNK_SIZE, len(text))))
        if len(spans) >= CHUNK_MAX or spans[-1][1] >= len(text):
            break
    return spans


class EmbeddingEnricher:
    """Stores a per-node embedding (+ chunk vectors for long notes), skipping nodes
    whose embed-text is unchanged."""
    name = "embedding"

    def __init__(self, client: EmbeddingClient) -> None:
        self._client = client

    def enrich(self, node: Node, kb: "KnowledgeBase", *,
               only: "set[str] | None" = None) -> "dict | None":
        # `only` names LLM passes; embedding has none and always re-embeds when its
        # text hash moved, so the filter does not apply here.
        # Re-read: the summary may have been written by LLMEnricher earlier in this same batch,
        # so the in-memory node handed to us by claim_enrichment can be stale.
        node = kb.get(node.id) or node
        summary = node.summary
        base = summary if (summary and summary.strip()) else node.content
        h = content_hash(node.title, base)
        text = _embed_text(node.title, base)
        spans = _chunk_spans(text)
        # The hash gates BOTH vectors; "long text but no chunks yet" re-runs a node
        # embedded before chunking existed (the backfill path is just: run me again).
        if h == node.embedded_hash and not (spans and kb.chunk_count(node.id) == 0):
            return None                  # embed-text unchanged -> no model call
        vector = self._client.embed([text])[0]
        kb.set_embedding(node.id, vector, embedded_hash=h)
        chunk_vecs = self._client.embed([text[s:e] for s, e in spans]) if spans else []
        kb.set_chunk_embeddings(
            node.id, [(s, e, v) for (s, e), v in zip(spans, chunk_vecs)])
        # Provenance: the served embed model is stashed on the client by the call above.
        provider = getattr(self._client, "provider", None)
        if not provider:
            return None
        model = getattr(self._client, "embed_model", None) or "?"
        return {"providers": [f"{provider}:{model}"]}


def build_embedding_enricher(env: dict) -> "EmbeddingEnricher | None":
    """Construct the embedding enricher unless disabled via KB_EMBED_ENABLED (default on).

    Set KB_EMBED_ENABLED to 0/false/no to skip embedding without removing other config.
    """
    flag = env.get("KB_EMBED_ENABLED", "1").strip().lower()
    if flag in ("0", "false", "no", ""):
        return None
    from kb_core.inference_routing import InferenceRouter   # deferred: no cost when disabled
    return EmbeddingEnricher(InferenceRouter(env).embedder())
