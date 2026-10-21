"""Pure parsing/placement helpers for the Obsidian adapter (no file I/O)."""

import re
from datetime import date, datetime, timezone

import frontmatter

from kb_core.paths import join_path, sanitize_slug

# Obsidian frontmatter is exactly a `---` fence on the very first line. python-frontmatter's
# own boundary is any line of 3+ dashes (`^-{3,}`), which mis-fires on decorative `------`
# separators and on a leading blank line — it then tries to YAML-parse prose and raises
# "mapping values are not allowed". We gate on the strict fence so only genuine frontmatter
# is parsed; everything else is pure body (a longer dash run is a horizontal rule, not a fence).
_FRONTMATTER_FENCE = re.compile(r"^---[ \t]*\r?\n")


def has_frontmatter(text: str) -> bool:
    """True iff the text opens with a genuine frontmatter fence (same gate as parse_note)."""
    return bool(_FRONTMATTER_FENCE.match(text))


def parse_note(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter metadata from the markdown body. Raises on bad YAML."""
    if not _FRONTMATTER_FENCE.match(text):
        return {}, text
    post = frontmatter.loads(text)
    return dict(post.metadata), post.content


def placement_for(rel_path: str) -> tuple[str, str]:
    """Map a vault-relative '.md' path to (parent_path, slug), sanitized to ltree labels.

    'Projects/KB MCP/design.md' -> ('Projects.KB_MCP', 'design')
    'note.md'                   -> ('', 'note')
    """
    parts = rel_path.replace("\\", "/").split("/")
    stem = parts[-1]
    if stem.lower().endswith(".md"):
        stem = stem[: -len(".md")]
    parent_path = ""
    for folder in parts[:-1]:
        parent_path = join_path(parent_path, sanitize_slug(folder))
    return parent_path, sanitize_slug(stem)


def derive_title(metadata: dict, body: str, stem: str) -> str:
    if metadata.get("title"):
        return str(metadata["title"])
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return stem


# Frontmatter keys that carry a note's real-world event time, in priority order:
# `date` (meetings), `completed_at` (todos), `created` (manually-authored notes).
# `due` is intent not an event; `updated`/`synced_at` are pipeline times.
_EVENT_KEYS = ("date", "completed_at", "created")
# A date embedded in a filename, e.g. "2025-11-04 - Kickoff" / "2025_11_04_…".
_STEM_DATE = re.compile(r"(20\d{2})[-_.](\d{2})[-_.](\d{2})")


def derive_event_at(metadata: dict, stem: str) -> datetime | None:
    """When the note's content HAPPENED (meeting date, todo completion, authoring date).

    Frontmatter wins over a filename-embedded date; file timestamps are never consulted
    (bulk file operations rewrite them — see migration 0031). An unparseable value falls
    through to the next signal. Naive values are pinned to UTC — day precision is all
    event-time ordering needs.
    """
    for key in _EVENT_KEYS:
        got = _coerce_event(metadata.get(key))
        if got is not None:
            return got
    m = _STEM_DATE.search(stem)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
        except ValueError:
            return None                       # "2026_99_99_…" names a file, not a date
    return None


def _coerce_event(value) -> datetime | None:
    if isinstance(value, datetime):           # before date: datetime IS a date
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):               # YAML parses bare ISO dates to date objects
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            got = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)
    return None


def derive_tags(metadata: dict) -> list[str]:
    raw = metadata.get("tags")
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []
