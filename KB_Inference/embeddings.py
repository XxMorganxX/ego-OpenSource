"""
Embedding service for a local embedding model served by Ollama.

Wraps `OllamaEmbeddings` (langchain_ollama) to turn text into vectors. Unlike the chat
`ConversationManager` this is **stateless** — there's no per-session history; every call
is independent. It shares the same Ollama instance (and `base_url`) as the chat model, so
the embedding model simply loads as a second resident model alongside Gemma.

Default model is **qwen3-embedding:0.6b** (1024-dim native, Matryoshka-truncatable, 32k
context). Configuration is read from the environment (see .env.example):

    OLLAMA_EMBED_MODEL, OLLAMA_BASE_URL, OLLAMA_EMBED_DIMENSIONS, OLLAMA_KEEP_ALIVE
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from typing import Any

from langchain_ollama import OllamaEmbeddings

# --- Configuration (env-overridable) ----------------------------------------

DEFAULT_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b")
DEFAULT_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
# Matryoshka truncation: qwen3-embedding can return fewer than its native 1024 dims, trading
# a little retrieval quality for smaller/faster vectors. Unset -> full native dimensionality.
_dims = os.getenv("OLLAMA_EMBED_DIMENSIONS")
DEFAULT_EMBED_DIMENSIONS: "int | None" = int(_dims) if _dims else None
# Keep the embedder resident between requests so batch vault sweeps don't pay a cold reload.
# Mirrors the chat model's OLLAMA_KEEP_ALIVE: -1 pins forever; also accepts "30m", "0", etc.
_keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "-1")
DEFAULT_KEEP_ALIVE: "int | str" = int(_keep_alive) if _keep_alive.lstrip("-").isdigit() else _keep_alive
# Ollama's embedding runner can be evicted (memory pressure from the resident chat model)
# and EOFs the request that finds it dead; it respawns on the next one. Retry transient
# failures instead of surfacing a 500 for work the following attempt would complete.
DEFAULT_EMBED_RETRIES = int(os.getenv("OLLAMA_EMBED_RETRIES", "2"))
# llama.cpp embedding (non-causal) models need the whole sequence inside one micro-batch,
# and Ollama spawns the qwen3 runner with n_ubatch=2048 tokens — longer inputs EOF the
# request (observed as /embed 500s on 20k+ char notes). Cap input length ourselves:
# 6000 chars ≈ 1700 tokens stays safely under the ubatch, and for embeddings the head of
# a document carries the gist. 0 disables the cap.
DEFAULT_EMBED_MAX_CHARS = int(os.getenv("OLLAMA_EMBED_MAX_CHARS", "6000"))
# Ollama loads the embedding runner at the model's native context (32,768 for qwen3-embedding),
# which reserved 4.0GB of unified memory for a 595M-param model (as reported by `ollama ps`).
# Inputs are already clipped to DEFAULT_EMBED_MAX_CHARS (~1,500 tokens), so most of
# that window is dead weight; 2048 covers the clip with headroom. The reclaimed memory is the
# headroom a less-quantized chat model needs. 0 -> leave at the model's default.
_embed_num_ctx = int(os.getenv("OLLAMA_EMBED_NUM_CTX", "2048"))
DEFAULT_EMBED_NUM_CTX: "int | None" = _embed_num_ctx if _embed_num_ctx > 0 else None

# Shares the chat server's logger so inference + embedding lines interleave under one name.
log = logging.getLogger("kb_inference")


class EmbeddingService:
    """Turns text into vectors via a local embedding model on Ollama (stateless)."""

    def __init__(
        self,
        model: str = DEFAULT_EMBED_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        *,
        dimensions: "int | None" = DEFAULT_EMBED_DIMENSIONS,
        keep_alive: "int | str" = DEFAULT_KEEP_ALIVE,
        retries: int = DEFAULT_EMBED_RETRIES,
        max_chars: int = DEFAULT_EMBED_MAX_CHARS,
        num_ctx: "int | None" = DEFAULT_EMBED_NUM_CTX,
        **embed_kwargs: Any,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.dimensions = dimensions
        self.keep_alive = keep_alive
        self.retries = max(0, retries)
        self.max_chars = max(0, max_chars)
        self.num_ctx = num_ctx
        kwargs: dict[str, Any] = dict(
            model=model, base_url=base_url, keep_alive=keep_alive, **embed_kwargs
        )
        if dimensions is not None:
            kwargs["dimensions"] = dimensions
        if num_ctx is not None:
            kwargs["num_ctx"] = num_ctx
        # validate_model_on_init left False (the default): like ConversationManager, the server
        # should still boot if the model isn't pulled yet — surface that via health(), not a crash.
        self.embedder = OllamaEmbeddings(**kwargs)

    # --- embedding ----------------------------------------------------------

    def _clip(self, texts: list[str]) -> list[str]:
        """Cap each text at max_chars (see DEFAULT_EMBED_MAX_CHARS). No-op when 0."""
        if not self.max_chars:
            return texts
        clipped = [t[: self.max_chars] for t in texts]
        n = sum(1 for t, c in zip(texts, clipped) if len(c) < len(t))
        if n:
            log.info("embed clip: model=%s truncated %d/%d text(s) to %d chars",
                     self.model, n, len(texts), self.max_chars)
        return clipped

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Synchronous batch embed. Returns one vector per input text, in order."""
        texts = self._clip(texts)
        t0 = self._log_request(texts, stream=False)
        for attempt in range(self.retries + 1):
            try:
                vectors = self.embedder.embed_documents(texts)
                break
            except Exception:
                if attempt == self.retries:
                    self._log_failure(t0)
                    raise
                self._log_retry(attempt)
                time.sleep(1.5 * (attempt + 1))
        self._log_response(t0, vectors)
        return vectors

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """Async batch embed. Returns one vector per input text, in order."""
        texts = self._clip(texts)
        t0 = self._log_request(texts, stream=False)
        for attempt in range(self.retries + 1):
            try:
                vectors = await self.embedder.aembed_documents(texts)
                break
            except Exception:
                if attempt == self.retries:
                    self._log_failure(t0)
                    raise
                self._log_retry(attempt)
                await asyncio.sleep(1.5 * (attempt + 1))
        self._log_response(t0, vectors)
        return vectors

    # --- logging ------------------------------------------------------------

    def _log_request(self, texts: list[str], *, stream: bool) -> float:
        log.info(
            "embed start: model=%s count=%d total_chars=%d dims=%s",
            self.model, len(texts), sum(len(t) for t in texts),
            self.dimensions if self.dimensions is not None else "native",
        )
        return time.perf_counter()

    def _log_response(self, t0: float, vectors: list[list[float]]) -> None:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        dim = len(vectors[0]) if vectors else 0
        log.info("embed done: model=%s count=%d dim=%d latency_ms=%d",
                 self.model, len(vectors), dim, latency_ms)

    def _log_failure(self, t0: float) -> None:
        log.exception("embed failed: model=%s after_ms=%d",
                      self.model, int((time.perf_counter() - t0) * 1000))

    def _log_retry(self, attempt: int) -> None:
        log.warning("embed attempt %d/%d failed: model=%s — retrying",
                    attempt + 1, self.retries + 1, self.model, exc_info=True)

    # --- diagnostics --------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Check Ollama reachability and whether the embedding model is present."""
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=5) as resp:
                data = json.loads(resp.read())
            models = [m.get("name") for m in data.get("models", [])]
            return {
                "ollama": "up",
                "base_url": self.base_url,
                "model": self.model,
                "model_available": self.model in models,
                "dimensions": self.dimensions,  # null = model's native dimensionality
            }
        except Exception as exc:  # noqa: BLE001 - report any failure to the caller
            return {"ollama": "down", "base_url": self.base_url, "error": str(exc)}


if __name__ == "__main__":
    # Quick standalone smoke test.
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-8s %(name)s | %(message)s")
    svc = EmbeddingService()
    print("health:", svc.health())
    vecs = svc.embed(["hello world", "the quick brown fox"])
    print(f"embedded {len(vecs)} texts, dim={len(vecs[0])}")
