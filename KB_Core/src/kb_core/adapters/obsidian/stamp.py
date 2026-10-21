"""Stamp a stable `id:` into a vault note's frontmatter — minimal-diff, never lossy.

This deliberately reverses the adapter's historical read-only stance (see adapter.py): to
give every note a move-proof identity we now *write* one `id:` line. Two properties keep that
safe against the externally-synced vault that originally motivated read-only:

  * The id is **deterministic** — `uuid5(namespace, vault-relative-path)` — so if an external
    sync (Obsidian Sync / iCloud) reverts the stamp, re-stamping the same file writes the
    *identical* value: idempotent, no identity churn.
  * The edit is **minimal** — a single `id:` line inserted after the opening fence (or a tiny
    new fence for a note with none). Existing YAML is never reserialized, so human formatting,
    key order, and comments survive untouched.

Caveat: a path-derived id means a brand-new file created at a path some deleted note once held
would inherit that path's id. Path reuse like that is rare; a committed Git id (the git package)
is the collision-proof upgrade when a note has Git history.
"""

from pathlib import Path
from uuid import UUID, uuid5

from kb_core.adapters.obsidian.parser import has_frontmatter, parse_note

# Dedicated namespace so a stamp is a pure function of the note's vault path and nothing else.
STAMP_NAMESPACE = UUID("a7c0ffee-57a3-4b0b-9a5f-0b57a3b0babe")


def mint_id(rel_path: str) -> str:
    """The deterministic id a note at `rel_path` should carry."""
    return str(uuid5(STAMP_NAMESPACE, rel_path))


def current_id(text: str) -> str | None:
    """The note's existing frontmatter id, or None. Raises only on genuinely bad YAML."""
    meta, _ = parse_note(text)
    val = meta.get("id")
    return str(val) if val else None


def stamp_text(text: str, new_id: str) -> str | None:
    """Return `text` with `id: new_id` inserted, or None if it already carries an id.

    Inserts a single line after the opening fence; prepends a minimal fence when the note has
    no frontmatter. Never rewrites existing YAML.
    """
    if current_id(text) is not None:
        return None
    if has_frontmatter(text):
        nl = text.index("\n")                      # has_frontmatter guarantees a newline
        return f"{text[:nl + 1]}id: {new_id}\n{text[nl + 1:]}"
    return f"---\nid: {new_id}\n---\n\n{text}"


def stamp_file(path: Path, rel_path: str) -> str:
    """Ensure the note at `path` carries an id; write one (minimal-diff) if absent.

    Returns the note's id (existing or freshly written). Raises on read/write/YAML failure so
    the caller can report-and-skip — a single bad note must never abort a sync.
    """
    text = path.read_text(encoding="utf-8")
    existing = current_id(text)
    if existing is not None:
        return existing
    new_id = mint_id(rel_path)
    stamped = stamp_text(text, new_id)
    if stamped is not None:                        # None only on a race where an id appeared
        path.write_text(stamped, encoding="utf-8")
    return new_id
