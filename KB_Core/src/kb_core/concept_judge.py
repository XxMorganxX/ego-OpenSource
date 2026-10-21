"""Prompt, schema, and parsing for the concept labeler. Pure — no DB/network. Mirrors
meaning_judge.py.

Turns one discovered community (entity names + memory snippets) into a name/kind/
description, or rejects it as incoherent. The GRAPH decides membership; this judge only
labels — or discards — what the graph found. It never adds or removes members."""
from __future__ import annotations

import json

from kb_core.prompts import shipped_prompt

CONCEPT_KINDS = ("project", "topic", "area", "event", "other")

CONCEPT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "coherent": {"type": "boolean"},
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": list(CONCEPT_KINDS)},
        "description": {"type": "string"},
    },
    "required": ["coherent", "name", "kind", "description"],
}

SYSTEM_FILE = "concept_judge.system.md"


def default_system() -> str:
    """The shipped system prompt with no overlay — for callers that hold no KnowledgeBase
    (tests, ad-hoc scripts). Production passes `system=kb.prompt(SYSTEM_FILE)`."""
    return shipped_prompt(SYSTEM_FILE)


def build_concept_prompt(entities: "list[tuple[str, str | None]]",
                         memories: "list[tuple[str, ...]]",
                         previous_name: "str | None" = None,
                         notes: "list[str] | None" = None, *,
                         system: "str | None" = None) -> "tuple[str, str]":
    """entities: (name, class_name|None) strongest-first; memories: (title, snippet[,
    folder]) — the optional folder breadcrumb (where the note is filed) renders as a
    [bracketed] prefix, deliberate signal the system prompt tells the labeler to use."""
    lines: "list[str]" = []
    if previous_name:
        lines.append(f"previous name: {previous_name}")
    if notes:
        lines.append("curation notes:")
        lines += [f"- {n}" for n in notes]
    if entities:
        lines.append("entities:")
        lines += [f"- {n} ({c})" if c else f"- {n}" for n, c in entities]
    if memories:
        lines.append("memories:")
        for m in memories:
            t, s, folder = m if len(m) == 3 else (m[0], m[1], "")
            line = f"{t}: {s}".rstrip(": ")
            lines.append(f"- [{folder}] {line}" if folder else f"- {line}")
    return (system if system is not None else default_system()), \
        "\n".join(lines) + "\n\nWhat is this cluster?"


def parse_concept(reply: str) -> dict:
    data = json.loads(reply)
    kind = str(data.get("kind", "other")).strip().lower()
    return {"coherent": bool(data["coherent"]),
            "name": str(data["name"]).strip(),
            "kind": kind if kind in CONCEPT_KINDS else "other",
            "description": str(data.get("description", "")).strip()}


def fallback_label(entities: "list[tuple[str, str | None]]") -> dict:
    """Structural placeholder when no inference client is available or a call failed:
    named after the strongest entities. Fail-open — concepts are derived and re-labeled
    every run, so an outage degrades naming, never structure."""
    names = [n for n, _ in entities[:3]]
    return {"coherent": True, "name": " / ".join(names) or "unlabeled cluster",
            "kind": "other", "description": ""}


def is_fallback_name(name: str) -> bool:
    """True when a concept name looks like fallback_label output (' / '-joined entity
    names or the empty-cluster placeholder). Such a name must never be offered back to
    the labeler as a previous-name hint — the prompt's 'keep the previous name' rule
    would make the placeholder sticky across runs."""
    return name == "unlabeled cluster" or " / " in name
