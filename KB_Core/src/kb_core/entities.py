"""Pure helpers for entity nodes: identity, naming, and tree placement.

An entity is a node (type='entity', source='entity') placed under entities.<entity_type>.
Its deterministic id derives from (entity_type, normalized name), so the same concept
dedups across the memories that mention it. Memory->entity links use the reserved
'mentions' relation_type, which is what distinguishes them from memory<->memory edges.
"""

import re
from uuid import UUID

from kb_core.paths import sanitize_slug

ENTITY_SOURCE = "entity"        # node `source` for entity nodes (never pruned by syncs)
ENTITY_TYPE = "entity"         # node `type` for entity nodes
ENTITY_ROOT = "entities"       # tree root under which entity nodes live
# `entities.entity_type` is a 2-value KIND discriminator (Phase 2): a node is either an
# emergent structural TYPE of thing ('class', a kind like organization/person/repo) or a
# specific named thing that is_a one or more such types ('instance'). Type membership of an
# instance is NO LONGER its entity_type — it's a many-to-many link to class nodes (entity_class_links).
ENTITY_CLASS = "class"         # entity_type marking a structural TYPE of thing (a kind, not a theme)
ENTITY_INSTANCE = "instance"   # entity_type marking a specific named thing that is_a one or more types
INSTANCE_ROOT = "entities.instances"   # flat tree parent for ALL instances (category is a link)
MENTIONS = "mentions"          # reserved relation_type: memory --mentions--> entity (instance)
BELONGS_TO = "belongs_to"      # reserved relation_type: memory --belongs_to--> category

# entity lifecycle (entities.status); see migration 0011.
STATUS_INACTIVE = "inactive"   # discovered, not yet recurred past the threshold
STATUS_ACTIVE = "active"       # established / offered as a category
STATUS_BLOCKED = "blocked"     # human-removed tombstone: never offered, staged, or revived

# Tokens a model parrots straight out of the schema/instructions instead of naming a real
# category. Matched case-insensitively as whole "words" within a candidate name.
_PLACEHOLDER_WORDS = {
    "category", "categories", "name", "names", "example", "examples", "placeholder",
    "new", "existing", "entity", "entities", "instance", "class", "classes", "todo",
    "none", "null", "n/a", "unknown", "value", "string", "type", "types", "confidence",
}


# Above these a class "name" is a runaway generation or a sentence glued into a slug, not a
# category: four real words plus separators fit comfortably under the total cap, and no word
# in a real category name approaches 25 letters ('electroencephalography' is 22).
MAX_CLASS_NAME_LEN = 60
MAX_CLASS_WORD_LEN = 25


def is_valid_class_name(name: str) -> bool:
    """Reject category names that are obvious model artifacts rather than real categories.

    The recurrence gate only filters *random* one-offs; a small model that consistently
    emits a placeholder like ``NEW_CATEGORY_NAME`` recurs right through it. This is the
    cheap, deterministic guard that stops such names before they ever cast a vote:
      - empty / whitespace, or no alphabetic character at all
      - longer than MAX_CLASS_NAME_LEN, or any single word beyond MAX_CLASS_WORD_LEN —
        a runaway generation ('aaaa…') or a sentence-as-slug, not a name
      - any character beyond letters, spaces, hyphens, apostrophes — rules out
        SCREAMING_SNAKE_CASE, "n/a", digit soup, and other schema/code artifacts
      - more than four words (a concept is a short noun phrase, up to four words).
        Hyphens count as word SEPARATORS here, so a degenerate hyphen-glued run-on
        ('type-of-a-feature-that-is-not-…') is counted at its real length instead of
        slipping through as one "word" — real hyphenated compounds ('co-workers') are
        short and still pass
      - an immediately repeated word ('feature feature') — a degeneration signature;
        legitimate non-adjacent repeats ('peer to peer network') still pass
      - made up entirely of placeholder/meta words ('category', 'new', 'example', …)
    """
    n = (name or "").strip()
    if not n or not any(c.isalpha() for c in n):
        return False
    # A single letter ('D') is never a category — real one-letter names (the C or R
    # language) are INSTANCES of a class, not classes. Instances keep allowing them.
    if sum(c.isalpha() for c in n) < 2:
        return False
    if len(n) > MAX_CLASS_NAME_LEN:
        return False
    if re.search(r"[^a-zA-Z\s'-]", n):
        return False
    words = re.findall(r"[a-zA-Z][a-zA-Z']*", n)
    if not words or len(words) > 4:
        return False
    if any(len(w) > MAX_CLASS_WORD_LEN for w in words):
        return False
    lowered = [w.lower() for w in words]
    if any(a == b for a, b in zip(lowered, lowered[1:])):
        return False
    if all(w in _PLACEHOLDER_WORDS for w in lowered):
        return False
    return True


# Generic/pronoun/group terms that are never a *specific* named entity. A name made up
# entirely of these is noise, not an instance. Kept tight on purpose — for instances a
# false reject (dropping a real entity) is worse than a miss, which the later LLM judge
# can still catch. Matched case-insensitively as whole words.
_INSTANCE_STOPWORDS = {
    "i", "me", "my", "we", "us", "our", "you", "your", "he", "him", "his", "she", "her",
    "they", "them", "their", "it", "its", "this", "that", "these", "those",
    "team", "teams", "group", "groups", "guy", "guys", "someone", "anyone", "everyone",
    "people", "others", "folks", "stuff", "thing", "things", "everything", "something",
}

# File extensions that mark a name as a FILE, not an entity (README.md, run_transfer.py, …).
_FILE_EXT_RE = re.compile(
    r"\.(md|markdown|py|pyc|ipynb|ts|tsx|js|jsx|sql|json|ya?ml|toml|txt|csv|tsv|sh|bash|"
    r"zsh|cfg|ini|env|lock|log|png|jpe?g|gif|svg|pdf|html?|css|scss)$",
    re.IGNORECASE,
)

# Above this an instance "name" is a runaway generation or a task-description slug
# (e.g. 'quarterly-review-ask-sam-about-the-draft-budget-figures'), not a real name.
MAX_INSTANCE_NAME_LEN = 40

# Code-symbol shapes that are NEVER a real entity name — the high-precision half of the
# instance gate. camelCase / PascalCase / plain snake_case are deliberately NOT here: those
# are the ambiguous middle the LLM meaning-judge decides, not a regex.
_SCREAMING_SNAKE_RE = re.compile(r"^[A-Z0-9]+(_[A-Z0-9]+)+$")
_ATTR_PATH_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
_CAMEL_SEG_RE = re.compile(r"[a-z][A-Z]")
_LOWER_RUN_RE = re.compile(r"[a-z]{3,}")


def code_symbol_issue(name: str) -> "str | None":
    """Why `name` is an unambiguous source-code symbol (never an entity), or None.

    High precision by design — only shapes that are never a real named thing. Anything a
    person might plausibly use as a name (camelCase, PascalCase, snake_case, hyphenated
    slugs) is left to the LLM meaning-judge, not rejected here.
    """
    n = (name or "").strip()
    if not n:
        return None
    if n.startswith("_"):
        return "leading-underscore code identifier"
    # A trailing numeric segment (COVID_19, GPT_4, US_EAST_1) is a plausible real name, not a
    # code constant — leave it to the judge.
    if _SCREAMING_SNAKE_RE.match(n) and not re.search(r"_[0-9]+$", n):
        return "SCREAMING_SNAKE_CASE constant"
    if "." in n and _ATTR_PATH_RE.match(n) and (n.count(".") >= 2 or _CAMEL_SEG_RE.search(n)):
        return "dotted attribute/method path"
    # Opaque = high-entropy interleaving (Slack ids like C01A2B3C4D5). A clean letters-then-digits
    # "brand+number" token (SHA256, RTX4090, ISO8601) is a plausible real name — spare it.
    if (re.fullmatch(r"[A-Za-z0-9]{6,}", n) and any(c.isdigit() for c in n)
            and any(c.isupper() for c in n) and not _LOWER_RUN_RE.search(n)
            and not re.fullmatch(r"[A-Za-z]+[0-9]+", n)):
        return "opaque identifier"
    return None


def instance_name_issue(name: str) -> str | None:
    """Why `name` is unfit to be an entity INSTANCE, or None if it's acceptable.

    The instance-level counterpart of `is_valid_class_name`, but deliberately more permissive
    on charset and word count: real instance names legitimately carry digits, hyphens, dots and
    >3 words ('Acme Robotics Club', 'claude-sonnet-4-6', 'atlas_dockV2'). It only
    rejects the high-confidence junk seen in practice — files, paths, task slugs, pronouns,
    placeholders — so a genuine low-signal name (a one-word person/org) still gets through.
    Returns a short, human-readable reason (used by the prune preview) or None.
    """
    n = (name or "").strip()
    if not n or not any(c.isalnum() for c in n):
        return "empty or has no alphanumeric character"
    if _FILE_EXT_RE.search(n):
        return "looks like a filename"
    if "/" in n or "\\" in n:
        return "looks like a path"
    if len(n) > MAX_INSTANCE_NAME_LEN:
        return f"too long ({len(n)} chars) — a slug/description, not a name"
    code = code_symbol_issue(n)
    if code is not None:
        return code
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", n)
    if words and all(w.lower() in _INSTANCE_STOPWORDS for w in words):
        return "generic/pronoun term, not a specific entity"
    if words and all(w.lower() in _PLACEHOLDER_WORDS for w in words):
        return "placeholder/meta word, not a real entity"
    return None


def is_valid_instance_name(name: str) -> bool:
    """True if `name` is fit to become an entity instance. See `instance_name_issue`."""
    return instance_name_issue(name) is None


# A class description is a one-line gloss the discover pass asks the small model to draft.
# Real ones are short and varied ("correspondence and messages received or sent"). Free text
# means it can degenerate: a weak model sometimes collapses into a repetition spiral
# (e.g. "…r_e_r_e_r_e…" for hundreds of chars) that the schema's bare `string` type happily
# accepts. This is the deterministic guard that drops such drafts before they reach the column.
MAX_CLASS_DESCRIPTION_LEN = 160   # real descriptions are one short line (~30–90 chars)


def clean_class_description(description: str | None) -> str | None:
    """Return a trimmed class description, or None if it's empty or a degenerate SLM draft.

    Two collapse signals, both deterministic and tuned to leave every real (short, varied)
    description untouched:
      - longer than ``MAX_CLASS_DESCRIPTION_LEN`` — the model is told to write one short line,
        so anything this long is a runaway generation, not a description;
      - a long, low-variety string (≥ 40 chars but ≤ 6 distinct characters) — the signature of
        a repetition spiral that stayed under the length cap.
    Returning None means "no usable description"; callers persist nothing, so the existing
    value (or NULL) is preserved rather than overwritten with noise.
    """
    text = (description or "").strip()
    if not text:
        return None
    if len(text) > MAX_CLASS_DESCRIPTION_LEN:
        return None
    if len(text) >= 40 and len(set(text.lower())) <= 6:
        return None
    return text


# A reserved pseudo-category for an instance the extractor could not place under any ACTIVE
# class (top-down: extraction only assigns to active types). Staged so the instance still
# materializes (node + mentions) with NO is_a link; a later pass links it once a type activates.
UNTYPED_CATEGORY = "__untyped__"


def normalize_entity_name(name: str) -> str:
    """Dedup key for an entity name: trimmed, lowercased, internal whitespace collapsed."""
    return re.sub(r"\s+", " ", name.strip().lower())


def canonical_key(name: str) -> str:
    """Identity key for an INSTANCE — more aggressive than normalize_entity_name: lowercased with
    every non-alphanumeric stripped, so casing/spacing/punctuation variants fold to one entity
    ('Vandelay', '__VANDELAY__', 'Vande Lay' -> 'vandelay'). This is what makes a real entity
    resolve to a single canonical node regardless of how it was written. Semantic variants that
    differ in actual letters ('Acme' vs 'Acme University') stay distinct — those are for the
    later LLM judge, not deterministic folding."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def entity_external_id(entity_type: str, name: str) -> str:
    """Stable external_id for a CLASS entity: 'class:{normalized_name}'. (Instances use
    instance_external_id — their identity is category-independent; see canonical_key.)"""
    return f"{entity_type}:{normalize_entity_name(name)}"


def instance_external_id(name: str) -> str:
    """Stable external_id for an INSTANCE: 'instance:{canonical_key}'. Category-independent, so the
    same concept is ONE node no matter which categories mention it."""
    return f"{ENTITY_INSTANCE}:{canonical_key(name)}"


def entity_parent_path(entity_type: str) -> str:
    """Tree parent for a CLASS entity, e.g. 'entities.people'. Instances live flat under
    INSTANCE_ROOT regardless of category (see instance_parent_path)."""
    return f"{ENTITY_ROOT}.{sanitize_slug(entity_type)}"


def instance_parent_path() -> str:
    """Tree parent for every instance — flat under 'entities.instances'. Category is a link now,
    not a tree location, so an instance no longer nests beneath a single category."""
    return INSTANCE_ROOT


def merge_pair(a: UUID, b: UUID) -> "tuple[UUID, UUID]":
    """Canonical (left, right) ordering for a merge pair: sorted by uuid text, so the same two
    nodes map to one row regardless of discovery direction. Used for candidates and blocks."""
    return (a, b) if str(a) < str(b) else (b, a)


# ---------- structural kind axis ----------
# A small closed STRUCTURAL axis over entities, mirroring concepts.kind. Open-vocab classes
# remain the domain taxonomy; kind answers "what sort of thing" for ranking/filtering/coverage.
KINDS = ("person", "organization", "place", "event", "project",
         "artifact", "concept", "system", "other")
KIND_EVENT = "event"

# Mention-role vocabulary: how an entity relates to the memory that mentions it.
ROLES = ("stakeholder", "owner", "participant", "subject", "tool-used")

def _levenshtein(a: str, b: str, cap: int = 3) -> int:
    """Plain DP edit distance, early-exited at `cap` (only tiny distances matter here)."""
    if abs(len(a) - len(b)) > cap:
        return cap
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
            best = min(best, cur[-1])
        if best >= cap:
            return cap
        prev = cur
    return min(prev[-1], cap)


def class_name_typo_of(name: str, existing: "list[str]") -> "str | None":
    """The existing class name that `name` is a spelling variant of, or None.

    Deterministic typo gate: a NEW class name within edit distance 1 (length 5-7)
    or 2 (length >= 8) of an existing class, sharing its first letter OR differing only by a
    leading insertion ('icompany'), is a misspelling — vote for the existing class instead of
    minting a fragment ('conpany'). Short names (< 5 letters) never fold: at that length one
    edit is a different word (plan/clan), not a typo. Exact matches return the existing name
    (callers treat that as 'already exists')."""
    n = normalize_entity_name(name)
    if not n:
        return None
    for ex in existing:
        e = normalize_entity_name(ex)
        if n == e:
            return ex
    if len(n) < 5:
        return None
    allowed = 1 if len(n) <= 7 else 2
    best: "tuple[int, str] | None" = None
    for ex in existing:
        e = normalize_entity_name(ex)
        if len(e) < 5:
            continue
        if n[0] != e[0] and n[1:] != e:      # allow a spurious leading char ('icompany')
            continue
        d = _levenshtein(n, e, cap=allowed + 1)
        if d <= allowed and (best is None or d < best[0]):
            best = (d, ex)
    return best[1] if best else None


# Leading lines that carry no evidence about an anchor: markdown headings (the title is
# emitted separately by the judge views), blank lines, and "**Key:** value" metadata rows
# such as an email's "**Participants:** ...". Skipping them means a 200-char snippet spends
# its budget on substance instead of a header block.
def _is_boilerplate_line(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#"):
        return True
    if s.startswith("**"):                     # "**Participants:** a@example.com, b@example.org"
        parts = s.split("**")
        return len(parts) >= 3 and parts[1].rstrip().endswith(":")
    return False


def mention_snippet(content: "str | None", limit: int = 200) -> str:
    """First substantive `limit` characters of a memory, for grounding a judge prompt.

    Skips leading headings, blank lines, and bold-key metadata rows. Falls back to the raw
    head of the text when every line is boilerplate, so a mention never contributes nothing.
    """
    if not content:
        return ""
    lines = content.splitlines()
    start = 0
    while start < len(lines) and _is_boilerplate_line(lines[start]):
        start += 1
    body = "\n".join(lines[start:]).strip()
    if not body:
        body = content.strip()
    return body[:limit]
