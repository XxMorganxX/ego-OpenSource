"""Claude API backend for the inference Protocol — an alternative to the local Gemma server.

Satisfies the same `InferenceClient.complete()` contract as `HTTPInferenceClient`, so the whole
enrichment pipeline is provider-agnostic: select it per task with `provider = "anthropic"` in
inference.toml (the task's `model`, default Sonnet). Each call is one stateless turn — system + user prompt
in, the model's reply text out.

NOTE: this provides completion only. Anthropic has no embeddings endpoint, so the embedding
enricher always runs against the local KB_Inference server regardless of this setting.
"""

import json

from kb_core.ingest.inference import InferenceError

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096


class AnthropicInferenceClient:
    """Talks to the Claude Messages API. One `complete()` = one stateless `messages.create`.

    The `client` arg is for tests (inject a fake); in production it's built lazily on first use
    so selecting this provider never needs an API key until a pass actually runs (mirrors the
    local client's non-fatal preflight).
    """

    provider = "anthropic"

    def __init__(self, *, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS,
                 api_key: "str | None" = None, client=None) -> None:
        self.model = model
        self._max_tokens = max_tokens
        self._api_key = api_key
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:                       # pragma: no cover - dep guard
                raise InferenceError(
                    "the 'anthropic' package is required for KB_INFERENCE_PROVIDER=anthropic"
                ) from exc
            self._client = Anthropic(api_key=self._api_key)  # None -> ANTHROPIC_API_KEY env
        return self._client

    def complete(self, system_prompt: str, user_prompt: str, *,
                 schema: "dict | None" = None) -> str:
        """One stateless LLM turn: system + user prompt -> reply text.

        With a `schema`, the JSON shape is appended to the system prompt so the reply matches it
        (Sonnet returns clean JSON reliably; the caller still parses via `_parse_json`). We don't
        use strict structured outputs because the staged schemas carry constraints/optional fields
        that strict mode rejects.
        """
        system = system_prompt
        if schema is not None:
            system = (f"{system_prompt}\n\nReply with ONLY a JSON value conforming to this JSON "
                      f"schema — no prose, no markdown fences:\n{json.dumps(schema)}")
        try:
            resp = self._ensure_client().messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except InferenceError:
            raise
        except Exception as exc:
            raise InferenceError(f"anthropic inference call failed: {exc}") from exc
        return "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", None) == "text").strip()
