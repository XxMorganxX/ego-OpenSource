"""Prompt, schema, parsing, and preview rendering for the NL concept-curation box.
Pure — no DB/network (mirrors meaning_judge.py / concept_judge.py).

NL is the INTERFACE, not the storage: the model compiles an instruction into structured
ops drawn from a closed vocabulary, referencing ONLY ids present in the server-built
roster. Curation is a write path, so this is fail-closed by construction — a reference
that isn't in the roster can't be expressed as a valid op, and the model is told to emit
a warning instead of guessing."""
from __future__ import annotations

import json

CURATION_OPS = ("rename", "set_kind", "set_description", "dismiss", "restore",
                "must_link", "cannot_link", "separate", "remove_member",
                "pin_member")

CURATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "ops": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": list(CURATION_OPS)},
                "concept_id": {"type": "string"},
                "node_id": {"type": "string"},
                "left_id": {"type": "string"},
                "right_id": {"type": "string"},
                "left_concept_id": {"type": "string"},
                "right_concept_id": {"type": "string"},
                "name": {"type": "string"},
                "kind": {"type": "string"},
                "description": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["op"],
        }},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ops"],
}

_SYSTEM = (
    "You translate one instruction about a personal knowledge base's CONCEPTS into "
    'structured curation ops. Answer strictly as JSON {"ops": [...], "warnings": [...]}.\n'
    "Ops: rename (concept_id, name) | set_kind (concept_id, kind: project|topic|area|"
    "event|other) | set_description (concept_id, description) | dismiss (concept_id) | "
    "restore (concept_id) | must_link (left_id, right_id: entity ids that belong in ONE "
    "concept) | cannot_link (left_id, right_id: entity ids that must NEVER share a "
    "concept) | separate (left_concept_id, right_concept_id: two CONCEPTS that are "
    "unrelated — future runs keep their clusters apart; use this when the instruction "
    "declares two concepts unrelated, not entities) | remove_member (concept_id, "
    "node_id) | pin_member (concept_id, node_id).\n"
    "Use ONLY ids from the roster below — never invent ids. When the instruction "
    "references something with no roster match, emit NO op for it and add a warning "
    "explaining what could not be resolved. Set each op's note to the relevant part of "
    "the user's own words (it is stored as provenance). Prefer the smallest set of ops "
    "that fulfils the instruction."
)


def build_curation_prompt(text: str, roster: dict) -> "tuple[str, str]":
    lines: "list[str]" = ["roster:"]
    lines += [f"concept {c['id']} :: {c['name']}" for c in roster.get("concepts", [])]
    lines += [f"entity {e['id']} :: {e['name']}" for e in roster.get("entities", [])]
    lines.append("")
    lines.append(f"instruction: {text}")
    return _SYSTEM, "\n".join(lines)


def parse_curation(reply: str) -> dict:
    data = json.loads(reply)
    ops = data.get("ops", [])
    for op in ops:
        if op.get("op") not in CURATION_OPS:
            raise ValueError(f"unknown curation op: {op.get('op')}")
    return {"ops": ops,
            "warnings": [str(w) for w in data.get("warnings", [])]}


def describe_op(op: dict, names: "dict[str, str]") -> str:
    """One human-readable preview line per op — what will happen if applied."""
    def nm(key: str) -> str:
        return names.get(str(op.get(key)), str(op.get(key)))

    k = op["op"]
    if k == "rename":
        return f"rename '{nm('concept_id')}' to '{op.get('name')}' and lock the label"
    if k == "set_kind":
        return f"set '{nm('concept_id')}' kind to {op.get('kind')} and lock the label"
    if k == "set_description":
        return f"rewrite the description of '{nm('concept_id')}' and lock the label"
    if k == "dismiss":
        return f"dismiss '{nm('concept_id')}' — future runs stop showing it"
    if k == "restore":
        return f"restore '{nm('concept_id')}'"
    if k == "must_link":
        return (f"must-link: '{nm('left_id')}' and '{nm('right_id')}' will always "
                "share one concept")
    if k == "cannot_link":
        return (f"cannot-link: '{nm('left_id')}' and '{nm('right_id')}' will never "
                "share a concept — the next run splits any concept containing both")
    if k == "separate":
        return (f"separate: '{nm('left_concept_id')}' and '{nm('right_concept_id')}' "
                "are unrelated — every future run keeps their clusters apart (a "
                "nested concept is split out)")
    if k == "remove_member":
        return f"remove '{nm('node_id')}' from '{nm('concept_id')}' (durable)"
    return f"pin '{nm('node_id')}' into '{nm('concept_id')}' (durable)"
