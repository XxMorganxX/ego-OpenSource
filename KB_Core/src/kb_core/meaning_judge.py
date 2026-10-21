"""Prompt, schema, and parsing for the 'is this a meaningful entity?' judge. Pure — no DB/network.

Mirrors merge_judge.py. Decides whether a recurring candidate is a useful semantic retrieval
anchor worth remembering or generic / implementation-only noise that should be dropped.
"""
from __future__ import annotations

import json

from kb_core.prompts import shipped_prompt

MEANING_SCHEMA: dict = {
    "type": "object",
    "properties": {"keep": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["keep", "reason"],
}

SYSTEM_FILE = "meaning_judge.system.md"


def default_system() -> str:
    """The shipped system prompt with no overlay — for callers that hold no KnowledgeBase
    (tests, ad-hoc scripts). Production passes `system=kb.prompt(SYSTEM_FILE)`."""
    return shipped_prompt(SYSTEM_FILE)


def build_meaning_prompt(name: str, entity_type: str, mentions: "list[str]", *,
                         system: "str | None" = None) -> "tuple[str, str]":
    lines = [f"name: {name}"]
    if entity_type:
        lines.append(f"proposed category: {entity_type}")
    if mentions:
        lines.append("context: " + " | ".join(mentions))
    user = "\n".join(lines) + "\n\nIs this a meaningful knowledge entity to keep?"
    return (system if system is not None else default_system()), user


def parse_meaning(reply: str) -> "tuple[bool, str]":
    data = json.loads(reply)
    return bool(data["keep"]), str(data.get("reason", "")).strip()
