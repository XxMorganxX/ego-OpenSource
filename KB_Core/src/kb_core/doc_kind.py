"""Pure helpers for the emergent document-kind facet — no DB or network (mirrors
entities.py / concepts.py). Guards constrain FORM only (length, degenerate generations,
placeholder meta-words) — never vocabulary: the kind taxonomy is open by design and
converges statistically."""
from __future__ import annotations

import re

# Kinds are short noun phrases ("meeting notes", "implementation plan"). Anything longer
# is a runaway generation or a description, not a kind.
MAX_DOC_KIND_LEN = 40

# A kind is "established" once this many live memories carry it. The threshold gates what
# aggregate surfaces (rollup / panel / prompt) show by default — never what persists: the
# long tail stays stored and filterable, it just isn't shown until it recurs.
DEFAULT_DOC_KIND_SUPPORT = 3

# "No signal" replies the model emits instead of the schema's empty string. Meta-words,
# not vocabulary — a genuine kind never consists of one of these alone.
_PLACEHOLDER_KINDS = frozenset({"unknown", "none", "n/a", "na", "other", "misc"})


def normalize_doc_kind(raw: "str | None") -> "str | None":
    """The kind fit to persist, or None meaning "persist nothing" (the caller keeps the
    existing value — the same fail-open posture as clean_class_description). Lowercases,
    trims, collapses internal whitespace; rejects empty/non-alphanumeric, placeholder
    meta-words, over-long runaways, and repetition spirals (>= 20 chars with <= 5 distinct
    characters — the same collapse signature clean_class_description catches)."""
    text = re.sub(r"\s+", " ", (raw or "").strip().lower())
    if not text or not any(c.isalnum() for c in text):
        return None
    if text in _PLACEHOLDER_KINDS:
        return None
    if len(text) > MAX_DOC_KIND_LEN:
        return None
    if len(text) >= 20 and len(set(text)) <= 5:
        return None
    return text
