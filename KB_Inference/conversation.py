"""
LangChain conversation manager for a local Gemma model served by Ollama.

Wraps `ChatOllama` and keeps **per-session** conversation context (the running
message history) plus a **per-session system prompt**.

Two ways to use it:

1. As a long-lived, multi-session manager (this is what the FastAPI server uses):

       manager = ConversationManager()
       reply = await manager.achat("alice", "Hello!")

2. As an ergonomic Python *context manager* for standalone / scripted use, which
   lets you set a system prompt and then carry conversation context across turns:

       manager = ConversationManager()
       with manager.session("demo", system_prompt="You are a terse pirate.") as chat:
           print(chat.send("Who are you?"))
           print(chat.send("And what's 2 + 2?"))   # remembers the prior turn

Configuration is read from the environment (see .env.example):
    OLLAMA_MODEL, OLLAMA_BASE_URL, OLLAMA_TEMPERATURE, DEFAULT_SYSTEM_PROMPT
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

# --- Configuration (env-overridable) ----------------------------------------

# Same default as the stack (kb-dev.sh, both .env.example files), so a standalone run
# serves the model `kb-dev` pulls rather than one nobody pulled.
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:12b-it-q8_0")
DEFAULT_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
# Low (not greedy) sampling: the KB's enrichment/extraction work wants stable, repeatable
# output, but a touch of temperature avoids the degenerate loops pure-greedy decoding can
# fall into. A fixed seed makes a given (prompt, temperature) reproducible across calls.
DEFAULT_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.3"))
DEFAULT_SEED = int(os.getenv("OLLAMA_SEED", "0"))
# Keep the model resident between requests so back-to-back ingestion sweeps don't pay a cold
# reload (a reload stacked behind queued calls is what tips a turn past the client timeout).
# -1 pins it forever; accepts any Ollama keep_alive value ("30m", "0" to unload immediately).
_keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "-1")
DEFAULT_KEEP_ALIVE: "int | str" = int(_keep_alive) if _keep_alive.lstrip("-").isdigit() else _keep_alive
# Context window. Unset -> Ollama's model default, 16,384 for gemma4:26b as reported by
# `ollama ps` (the model's native maximum is 262,144). Lowering it shrinks the
# KV cache (eases memory pressure) but truncates long inputs, so leave it unset unless inputs
# are known-small.
_num_ctx = os.getenv("OLLAMA_NUM_CTX")
DEFAULT_NUM_CTX: "int | None" = int(_num_ctx) if _num_ctx else None
# Output cap. Bounds a degenerate decode loop: uncapped, a looping generation runs until the
# caller's 300s timeout and its work is discarded anyway. A small share of uncapped calls run
# away to very long outputs, far past the typical reply length.
#
# This counts TOTAL generated tokens, INCLUDING gemma4:26b's chain-of-thought — which the
# `reply_chars` figure in the log does NOT (it records only the final reply text). Sizing this
# from reply_chars is how an earlier 1024 default silently truncated every extraction: the
# model spent the whole budget thinking and emitted no JSON. Measured uncapped, a typical
# extract_entities call generates ~2,200-3,300 tokens total (thinking 7.5-12k chars, content
# under 1.3k); the runaways were >10,800 content tokens alone. 8192 clears normal work with
# margin and still catches them. 0 -> uncapped.
_num_predict = int(os.getenv("OLLAMA_NUM_PREDICT", "8192"))
DEFAULT_NUM_PREDICT: "int | None" = _num_predict if _num_predict > 0 else None
# Chain-of-thought on/off. Ollama calls this option `think`; ChatOllama exposes it as
# `reasoning`, which is what we set below — the env var is named for the wire option so it
# matches Ollama's own docs when debugging.
#
# THREE states, and the third is not the same as the second: unset leaves the decision to the
# model (gemma4 thinks by default), so unset and "off" behave differently. Only pass the kwarg
# when the operator asked for something, or a future model's default is silently overridden.
#
# Why it is a knob at all: thinking dominates the cost of extract_entities. Measured,
# one call runs 100-175s with thinking on and ~7s with it off, and the thinking
# tokens are what exhaust num_ctx in the runaway cases. The eval's 2x2 varies this against the
# model to find out what the thinking is actually buying.
def _parse_think(value: "str | None") -> "bool | None":
    """`OLLAMA_THINK` -> True / False / None (leave it to the model).

    Unset and empty both mean "don't decide" — an operator who has not set the variable must
    not have the model's default silently overridden.
    """
    if value is None or not value.strip():
        return None
    return value.strip().lower() in ("1", "true", "on", "yes")


DEFAULT_REASONING: "bool | None" = _parse_think(os.getenv("OLLAMA_THINK"))
DEFAULT_SYSTEM_PROMPT = os.getenv("DEFAULT_SYSTEM_PROMPT", "You are a helpful assistant.")
# Sessions live in memory; past this many the least recently used one is dropped, so a
# caller that never purges (or a flood of fresh ids) cannot grow the process forever.
DEFAULT_MAX_SESSIONS = int(os.getenv("KB_INFERENCE_MAX_SESSIONS", "256"))

# Every model invocation is logged here. Output/level are configured by the process
# entrypoint (server.py); this module only emits. INFO = one start + one done line per
# turn with timing; DEBUG additionally logs capped previews of the prompt and reply.
log = logging.getLogger("kb_inference")


def _text(content: Any) -> str:
    """Normalize a LangChain message `.content` (str | list parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Some providers return content as a list of parts; join the text bits.
        return "".join(
            part if isinstance(part, str) else str(part.get("text", part))
            for part in content
        )
    return str(content)


def _preview(text: str, limit: int = 200) -> str:
    """A single-line, length-capped snippet of text, for DEBUG logging of prompts/replies."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


def _truncated(reply: Any) -> bool:
    """True when Ollama stopped because it hit num_predict (`done_reason == "length"`).

    The authoritative signal — reply length in chars can't distinguish a long-but-complete
    answer from a capped one. Absent metadata reads as not-truncated.
    """
    meta = getattr(reply, "response_metadata", None) or {}
    return meta.get("done_reason") == "length"


def _dump(message: BaseMessage) -> dict[str, str]:
    """Serialize a stored message to a {role, content} dict for the API."""
    role = {"human": "user", "ai": "assistant", "system": "system"}.get(
        message.type, message.type
    )
    return {"role": role, "content": _text(message.content)}


@dataclass
class Session:
    """One conversation: a system prompt plus its running message history."""

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    history: InMemoryChatMessageHistory = field(default_factory=InMemoryChatMessageHistory)
    # Serializes concurrent async turns on the same session so history stays consistent.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Whether the LAST turn on this session hit the output cap. Read by /chat so a caller
    # can tell a capped reply from a merely malformed one — the eval harness scores the two
    # differently, and only this distinguishes them.
    last_truncated: bool = False

    def build_messages(self, user_message: str) -> list[BaseMessage]:
        """Full prompt for the model: system prompt + prior history + new turn.

        Messages are constructed directly (not via string templates) so arbitrary
        user content — braces, code, JSON — is passed through verbatim and never
        interpreted as a template.
        """
        messages: list[BaseMessage] = []
        if self.system_prompt:
            messages.append(SystemMessage(self.system_prompt))
        messages.extend(self.history.messages)
        messages.append(HumanMessage(user_message))
        return messages


class ConversationManager:
    """Multi-session chat manager backed by a local Gemma model via Ollama."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = DEFAULT_TEMPERATURE,
        seed: int = DEFAULT_SEED,
        *,
        keep_alive: "int | str" = DEFAULT_KEEP_ALIVE,
        num_ctx: "int | None" = DEFAULT_NUM_CTX,
        num_predict: "int | None" = DEFAULT_NUM_PREDICT,
        reasoning: "bool | None" = DEFAULT_REASONING,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        **llm_kwargs: Any,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.seed = seed
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.reasoning = reasoning
        self._llm_kwargs = llm_kwargs
        self.llm = self._build()
        self.max_sessions = max(1, max_sessions)
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()

    def _build(self, format: Any | None = None) -> ChatOllama:
        """Construct a ChatOllama with the manager's shared settings.

        `keep_alive`, and (optional) `num_ctx` / `num_predict` / `reasoning`, apply to every
        model the manager builds.
        `format` (optional) is Ollama's structured-output control — the string "json" or a
        JSON Schema dict — forcing the reply to match it.
        """
        kwargs: dict[str, Any] = dict(
            model=self.model,
            base_url=self.base_url,
            temperature=self.temperature,
            seed=self.seed,
            keep_alive=self.keep_alive,
            **self._llm_kwargs,
        )
        if self.num_ctx is not None:
            kwargs["num_ctx"] = self.num_ctx
        if self.num_predict is not None:
            kwargs["num_predict"] = self.num_predict
        if self.reasoning is not None:
            kwargs["reasoning"] = self.reasoning
        if format is not None:
            kwargs["format"] = format
        return ChatOllama(**kwargs)

    def _llm_for(self, format: Any | None) -> ChatOllama:
        """The base model, or a structured-output variant constrained to `format`."""
        if format is None:
            return self.llm
        return self._build(format)

    # --- session bookkeeping ------------------------------------------------

    def _get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = Session()
            self._sessions[session_id] = session
            while len(self._sessions) > self.max_sessions:
                evicted, _ = self._sessions.popitem(last=False)
                log.info("session store full (%d): dropped least recently used %r",
                         self.max_sessions, evicted)
        else:
            self._sessions.move_to_end(session_id)
        return session

    def exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    def list_sessions(self) -> list[str]:
        return list(self._sessions)

    def set_system_prompt(self, session_id: str, prompt: str) -> None:
        self._get(session_id).system_prompt = prompt

    def get_system_prompt(self, session_id: str) -> str:
        return self._get(session_id).system_prompt

    def get_history(self, session_id: str) -> list[dict[str, str]]:
        return [_dump(m) for m in self._get(session_id).history.messages]

    def was_truncated(self, session_id: str) -> bool:
        """Whether the last turn on this session hit the output cap."""
        return self._get(session_id).last_truncated

    def reset(self, session_id: str) -> None:
        """Clear the conversation history but keep the system prompt."""
        self._get(session_id).history.clear()

    def delete(self, session_id: str) -> bool:
        """Forget a session entirely (prompt + history). Returns True if it existed."""
        return self._sessions.pop(session_id, None) is not None

    # --- inference logging --------------------------------------------------

    def _log_request(self, session_id: str, session: "Session", message: str, *,
                     structured: bool, stream: bool) -> float:
        """Log the start of a model invocation; return a perf-counter start time."""
        log.info(
            "inference start: session=%s model=%s structured=%s stream=%s "
            "history_msgs=%d prompt_chars=%d",
            session_id, self.model, structured, stream,
            len(session.history.messages), len(message),
        )
        log.debug("inference prompt: session=%s text=%r", session_id, _preview(message))
        return time.perf_counter()

    def _log_response(self, session_id: str, t0: float, text: str, *,
                      ttft: float | None = None, truncated: bool = False) -> None:
        """Log a completed invocation with latency, reply size, truncation, and (streams) TTFT."""
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if ttft is None:
            log.info("inference done: session=%s model=%s latency_ms=%d reply_chars=%d "
                     "truncated=%s",
                     session_id, self.model, latency_ms, len(text), truncated)
        else:
            log.info("inference done: session=%s model=%s ttft_ms=%d latency_ms=%d "
                     "reply_chars=%d truncated=%s",
                     session_id, self.model, int(ttft * 1000), latency_ms, len(text), truncated)
        log.debug("inference reply: session=%s text=%r", session_id, _preview(text))

    def _log_failure(self, session_id: str, t0: float) -> None:
        """Log a failed invocation (call from inside an `except` — includes traceback)."""
        log.exception("inference failed: session=%s model=%s after_ms=%d",
                      session_id, self.model, int((time.perf_counter() - t0) * 1000))

    # --- chat ---------------------------------------------------------------

    def chat(self, session_id: str, message: str, *, format: Any | None = None) -> str:
        """Synchronous chat turn. Appends both sides to the session history.

        `format` (optional) enforces a structured reply — "json" or a JSON Schema dict.
        """
        session = self._get(session_id)
        t0 = self._log_request(session_id, session, message,
                               structured=format is not None, stream=False)
        try:
            reply = self._llm_for(format).invoke(session.build_messages(message))
        except Exception:
            self._log_failure(session_id, t0)
            raise
        text = _text(reply.content)
        self._log_response(session_id, t0, text, truncated=_truncated(reply))
        session.last_truncated = _truncated(reply)
        session.history.add_user_message(message)
        session.history.add_ai_message(text)
        return text

    async def achat(self, session_id: str, message: str, *,
                    format: Any | None = None) -> str:
        """Async chat turn. Appends both sides to the session history.

        `format` (optional) enforces a structured reply — "json" or a JSON Schema dict.
        """
        session = self._get(session_id)
        async with session.lock:
            t0 = self._log_request(session_id, session, message,
                                   structured=format is not None, stream=False)
            try:
                reply = await self._llm_for(format).ainvoke(session.build_messages(message))
            except Exception:
                self._log_failure(session_id, t0)
                raise
            text = _text(reply.content)
            self._log_response(session_id, t0, text, truncated=_truncated(reply))
            session.last_truncated = _truncated(reply)
            session.history.add_user_message(message)
            session.history.add_ai_message(text)
            return text

    async def astream(self, session_id: str, message: str) -> AsyncIterator[str]:
        """Async token stream. History is committed once the stream completes."""
        session = self._get(session_id)
        async with session.lock:
            t0 = self._log_request(session_id, session, message,
                                   structured=False, stream=True)
            ttft: float | None = None
            parts: list[str] = []
            last_chunk: Any = None
            try:
                async for chunk in self.llm.astream(session.build_messages(message)):
                    last_chunk = chunk
                    piece = _text(chunk.content)
                    if piece:
                        if ttft is None:
                            ttft = time.perf_counter() - t0
                        parts.append(piece)
                        yield piece
            except Exception:
                self._log_failure(session_id, t0)
                raise
            text = "".join(parts)
            self._log_response(session_id, t0, text, ttft=ttft,
                               truncated=_truncated(last_chunk))
            session.last_truncated = _truncated(last_chunk)
            session.history.add_user_message(message)
            session.history.add_ai_message(text)

    # --- ergonomic context manager -----------------------------------------

    @contextmanager
    def session(
        self, session_id: str = "default", system_prompt: str | None = None
    ) -> Iterator["BoundSession"]:
        """Scope a session for standalone use; optionally (re)set its system prompt.

        The session persists in the manager after the block exits, so it can be
        resumed later or shared with the HTTP server.
        """
        if system_prompt is not None:
            self.set_system_prompt(session_id, system_prompt)
        else:
            self._get(session_id)  # ensure it exists
        yield BoundSession(self, session_id)

    # --- diagnostics --------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Check that Ollama is reachable and the configured model is present."""
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=5) as resp:
                data = json.loads(resp.read())
            models = [m.get("name") for m in data.get("models", [])]
            return {
                "ollama": "up",
                "base_url": self.base_url,
                "model": self.model,
                "model_available": self.model in models,
                "models": models,
            }
        except Exception:  # noqa: BLE001 - any failure means "down"
            # The detail goes to the log, not the response: /health answers anything
            # allowed to reach the server, and an exception string can carry internals.
            log.warning("ollama health probe failed", exc_info=True)
            return {"ollama": "down", "base_url": self.base_url,
                    "error": "ollama unreachable"}


@dataclass
class BoundSession:
    """A thin handle that binds chat calls to one session id (returned by `session()`)."""

    manager: ConversationManager
    session_id: str

    def send(self, message: str) -> str:
        return self.manager.chat(self.session_id, message)

    async def asend(self, message: str) -> str:
        return await self.manager.achat(self.session_id, message)

    def stream(self, message: str) -> AsyncIterator[str]:
        return self.manager.astream(self.session_id, message)

    def set_system_prompt(self, prompt: str) -> None:
        self.manager.set_system_prompt(self.session_id, prompt)

    def reset(self) -> None:
        self.manager.reset(self.session_id)

    @property
    def history(self) -> list[dict[str, str]]:
        return self.manager.get_history(self.session_id)


if __name__ == "__main__":
    # Quick standalone smoke test of the context manager.
    manager = ConversationManager()
    print("health:", manager.health())
    with manager.session("cli", system_prompt="You are a terse assistant. One sentence.") as chat:
        print("A:", chat.send("In one sentence, what is Ollama?"))
        print("B:", chat.send("And who makes the model I'm running?"))
        print("history:", chat.history)
