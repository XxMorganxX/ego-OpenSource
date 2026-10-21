"""Prompt, schema, and parsing for the entity 'describe' step. Pure — no DB/network.

Drafts the one-line description for an entity born without one (instances always,
classes as a backstop). Grounded in the entity's mention snippets; the daemon retries
blank entities on later ticks, so a garbled reply here just means "not this tick".
"""
from __future__ import annotations

import json

from kb_core.entities import ENTITY_CLASS

DESCRIBE_SCHEMA: dict = {
    "type": "object",
    "properties": {"description": {"type": "string"}},
    "required": ["description"],
}

# One short line, well under entities.MAX_CLASS_DESCRIPTION_LEN so a faithful draft
# always survives clean_class_description.
_SYSTEM_INSTANCE = (
    "You write a one-line description of a semantic retrieval anchor. The anchor may be a "
    "specific named person, organization, artifact, system, or resource, or it may be an "
    "abstract concept, method, principle, pattern, problem area, or skill. Say what or who the "
    "anchor is, or what the concept means, in one short clause of at most 120 characters. "
    "Ground the description only in the provided context, but do not merely summarize a mention "
    "or repeat the name; when context is thin, state the role it plays there. Each 'appears in' "
    "line may begin with the folder the note is filed under in [brackets] — where the mentioning "
    "notes live is strong evidence of what the anchor is and the project or area it belongs to. "
    "No preamble. "
    'Answer strictly as JSON {"description": "..."}.'
)
_SYSTEM_CLASS = (
    "You write a one-line dictionary definition of a broad knowledge-base category. Define what "
    "kind of thing belongs in the category in one short clause of at most 120 characters. The "
    "description must be a reusable category definition, not a summary of the supplied mentions "
    "or their incidental topics. Use the context only to disambiguate the category. No preamble. "
    'Answer strictly as JSON {"description": "..."}.'
)


def build_describe_prompt(view: dict, kind: str) -> "tuple[str, str]":
    """System + user prompts for one entity. `view` is _entity_judge_view output."""
    lines = [f"name: {view['name']}"]
    if view.get("aliases"):
        lines.append("aliases: " + ", ".join(view["aliases"]))
    if view.get("mentions"):
        lines.append("appears in:")
        lines.extend(f"- {m}" for m in view["mentions"])
    user = "\n".join(lines) + "\n\nWrite the one-line description."
    return (_SYSTEM_CLASS if kind == ENTITY_CLASS else _SYSTEM_INSTANCE), user


def parse_describe(reply: str) -> "str | None":
    """Pull `description` out of the model reply; None for a missing/garbled reply."""
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    text = str(data.get("description") or "").strip()
    return text or None
