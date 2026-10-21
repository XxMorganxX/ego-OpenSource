"""VaultDecoder: a pushed vault note -> NodeRecord, placed exactly as ObsidianAdapter would.

The agent sends the raw file text and the vault-relative path; every derivation (title,
tags, event time, ltree placement) runs HERE with the Obsidian parser's own functions, so
a pushed vault and a local vault produce byte-identical records for the same file.
"""
from __future__ import annotations

from datetime import datetime, timezone

from kb_core.adapters.obsidian.parser import (derive_event_at, derive_tags, derive_title,
                                              parse_note, placement_for)
from kb_core.domain import NodeRecord
from kb_core.paths import join_path, sanitize_slug

MAX_TEXT_BYTES = 1_048_576


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        got = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return got if got.tzinfo else got.replace(tzinfo=timezone.utc)


def _frontmatter_error(exc: Exception) -> str:
    """A frontmatter failure as the exception class and the line number only. PyYAML's own
    text quotes the note (an undefined alias, an unknown tag, a snippet of the line), and
    this message reaches t.<slug>.ingest.log, the inbox row and the dashboard, and no note
    content may appear in server logs. Line = 1-based within the frontmatter."""
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    line = getattr(mark, "line", None)
    where = f" at frontmatter line {line + 1}" if isinstance(line, int) else ""
    return f"frontmatter: {type(exc).__name__}{where}"


class VaultDecoder:
    source = "vault"

    def __init__(self, root_label: str | None = "Obsidian") -> None:
        # Sanitized exactly as ObsidianAdapter does, so the layer label is the same ltree label.
        self._root_label = sanitize_slug(root_label) if root_label else None

    def decode(self, payload: dict) -> NodeRecord:
        external_id = str(payload.get("external_id") or "")
        if not external_id:
            raise ValueError("external_id required")
        rel = str(payload.get("rel_path") or "").replace("\\", "/")
        if not rel or not rel.lower().endswith(".md"):
            raise ValueError("rel_path must be a vault-relative .md path")
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("text required")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("text over 1 MiB")
        # Pulls read via Path.read_text() (universal newlines: \r\n and lone \r become \n); match
        # it so a pushed CRLF/CR note yields the same content (and content hash) as the pulled one.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        parent_path, slug = placement_for(rel)             # ValueError on an unsanitizable name
        if self._root_label:
            parent_path = join_path(self._root_label, parent_path) if parent_path else self._root_label
        try:
            metadata, body = parse_note(text)
        except Exception as exc:                            # bad YAML: fail this event only
            raise ValueError(_frontmatter_error(exc)) from None
        stem = rel.rsplit("/", 1)[-1]
        stem = stem[:-3] if stem.lower().endswith(".md") else stem
        title = derive_title(metadata, body, stem)
        content = body.strip() or title
        mtime = _ts(payload.get("mtime")) or datetime.now(timezone.utc)
        birth = _ts(payload.get("birthtime")) or mtime
        return NodeRecord(
            source=self.source, external_id=external_id, type="note",
            content=content, slug=slug, parent_path=parent_path, title=title,
            tags=derive_tags(metadata), uri=f"vault://{rel}",
            source_created_at=birth, source_updated_at=mtime,
            event_at=derive_event_at(metadata, stem),
        )
