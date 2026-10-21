"""Pure assembly of embed-text: an entity's text, and the instructed form of a search query.

An entity's vector is grounded in *who mentions it*, not just its name — that is what
disambiguates short names ('Apple', 'JP'). No DB or network here; independently testable.
"""
from __future__ import annotations

import hashlib


def build_embed_text(
    name: str,
    aliases: "list[str]",
    description: "str | None",
    mentions: "list[tuple[str, str]]",
    *,
    kind: str,
    members: "list[str] | None" = None,
    budget: int = 2000,
) -> str:
    """One bounded text block for an entity. `mentions` is (title, snippet) pairs; `members`
    (class only) is member instance names. Whole result truncated to `budget` chars."""
    parts: list[str] = [name.strip()]
    if aliases:
        parts.append("aka: " + ", ".join(a.strip() for a in aliases if a.strip()))
    if description and description.strip():
        parts.append(description.strip())
    if kind == "class" and members:
        parts.append("members: " + ", ".join(m.strip() for m in members if m.strip()))
    for title, snippet in mentions:
        line = f"- {title.strip()}: {snippet.strip()}".rstrip(": ").rstrip()
        parts.append(line)
    return "\n".join(parts)[:budget]


def embed_text_hash(text: str) -> str:
    """Stable content hash of the assembled embed-text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The query side of qwen3-embedding's asymmetric retrieval format: documents are
# embedded raw (title + text), queries carry this instruction. Byte-exact on purpose:
# it is the string the CONFIDENCE_* pair in knowledge_base.py was derived with.
# Changing ANY
# byte -- the space after "Query:" included -- means bumping QUERY_INSTRUCTION_VERSION
# and re-deriving that pair (evals/retrieval/shadow_confidence.py). Re-decide on any
# embedding-model change.
QUERY_INSTRUCTION_VERSION = "instructed-v1"
QUERY_INSTRUCTION = ("Instruct: Given a web search query, retrieve relevant passages "
                     "that answer the query\nQuery: {q}")


def instruct_query(q: str) -> "str | None":
    """The instructed form of a search query, or None for a blank one. An instructed
    blank is never blank -- it would embed the instruction alone and return real rows
    with real cosines -- so a blank query stays unembeddable, as it is at /embed."""
    if not q or not q.strip():
        return None
    return QUERY_INSTRUCTION.format(q=q)
