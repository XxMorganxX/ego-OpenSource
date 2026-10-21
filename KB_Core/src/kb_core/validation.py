"""Contract enforcement — runs before any DB write."""

from kb_core.domain import NodeRecord
from kb_core.paths import sanitize_slug
from kb_core.registry import TypeRegistry


class ValidationError(ValueError):
    pass


# Postgres `text` cannot hold U+0000 at all: psycopg rejects the row client-side ("PostgreSQL
# text fields cannot contain NUL (0x00) bytes") before it is sent. Nothing between an adapter
# and the INSERT used to handle that, so a note carrying a literal NUL would fail every sync
# tick and never land. The engine's write paths swap it for U+2400, the Unicode SYMBOL FOR NULL
# — visible, unambiguous, and stable across re-syncs of the same bytes.
NUL_REPLACEMENT = "␀"


def sanitize_text(text: str | None) -> str | None:
    """Replace U+0000 with the visible SYMBOL FOR NULL (␀); None passes through."""
    if text is None or "\x00" not in text:
        return text
    return text.replace("\x00", NUL_REPLACEMENT)


def validate(record: NodeRecord, registry: TypeRegistry) -> None:
    if not record.source:
        raise ValidationError("source is required")
    if not record.external_id:
        raise ValidationError("external_id is required")
    if not record.type:
        raise ValidationError("type is required")
    if not record.content or not record.content.strip():
        raise ValidationError("content must be non-empty")
    if not record.slug:
        raise ValidationError("slug is required")
    try:
        sanitize_slug(record.slug)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not registry.is_registered(record.type):
        raise ValidationError(f"unknown type: {record.type}")
