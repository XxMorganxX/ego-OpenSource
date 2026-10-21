"""Prompt, schema, and parsing for the 'same entity?' merge judge. Pure — no DB/network."""
from __future__ import annotations

import json

JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {"same": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["same", "reason"],
}

_SYSTEM = (
    "You decide whether two knowledge-base entities identify the SAME semantic anchor: the same "
    "person, organization, product, project, artifact, resource, event, or abstract concept. "
    "Aliases, abbreviations, expanded names, and harmless spelling variants may be the same when "
    "the supplied context supports that identity. "
    "Context items may begin with the folder each note is filed under in [brackets] — folders "
    "show the domain a name is used in, and are strong evidence: mentions filed under the same "
    "or related folders support a shared identity, while consistently unrelated folders count "
    "against it. "
    "A 'Pair evidence' section, when present, is computed from the knowledge graph: two PEOPLE "
    "or ORGANIZATIONS co-mentioned in the same memories are almost always distinct actors — "
    "strong evidence AGAINST identity (weaker for tools/products, where an alias and its "
    "expansion are often used side by side); sharing many other co-mentioned entities supports "
    "identity when the names are similar. "
    "Merely related is NOT the same. Answer false — and name the relation in your reason — when "
    "one is a version of the other, a component or subsystem of the other, a tool vs the "
    "platform it runs on, an organization vs a group, club, or venue within it, or a person vs "
    "an organization they belong to. "
    'Answer strictly as JSON {"same": bool, "reason": one short sentence}. Be conservative: '
    "if ambiguous, answer false."
)


def _fmt(e: dict) -> str:
    lines = [f"name: {e['name']}"]
    if e.get("aliases"):
        lines.append("aliases: " + ", ".join(e["aliases"]))
    if e.get("description"):
        lines.append("description: " + e["description"])
    if e.get("mentions"):
        lines.append("context: " + " | ".join(e["mentions"]))
    return "\n".join(lines)


def build_judge_prompt(left: dict, right: dict,
                       evidence: "dict | None" = None) -> "tuple[str, str]":
    parts = [f"Entity A:\n{_fmt(left)}", f"Entity B:\n{_fmt(right)}"]
    if evidence:
        ev = [f"- co-mentioned together in {evidence.get('co_mentions', 0)} "
              "shared memories",
              f"- share {evidence.get('shared_co_entities', 0)} other "
              "co-mentioned entities"]
        if evidence.get("via"):
            ev.append(f"- surfaced by {evidence['via']} similarity")
        parts.append("Pair evidence:\n" + "\n".join(ev))
    user = "\n\n".join(parts) + "\n\nAre A and B the same entity?"
    return _SYSTEM, user


def parse_judge(reply: str) -> "tuple[bool, str]":
    data = json.loads(reply)
    return bool(data["same"]), str(data.get("reason", "")).strip()
