"""Pure helpers for ltree paths and slug labels. No database access."""

import re

_BAD_LABEL_CHARS = re.compile(r"[^A-Za-z0-9_]+")


def join_path(parent_path: str, slug: str) -> str:
    """Append a slug label to a parent ltree path. Empty parent -> root level."""
    return f"{parent_path}.{slug}" if parent_path else slug


def parent_of(path: str) -> str:
    """The parent path, or '' if `path` is top-level."""
    head, _, _ = path.rpartition(".")
    return head


def labels(path: str) -> list[str]:
    """The individual labels of a path, [] for the empty path."""
    return path.split(".") if path else []


def leaf(path: str) -> str:
    """The final label of a path."""
    return path.rpartition(".")[2]


def ancestor_paths(path: str) -> list[str]:
    """All proper ancestor paths, root-first. 'a.b.c' -> ['a', 'a.b']."""
    parts = labels(path)
    return [".".join(parts[: i + 1]) for i in range(len(parts) - 1)]


def humanize_path(path: str | None) -> str:
    """An ltree path as a readable breadcrumb for LLM prompts, e.g.
    'projects.kb_mcp.Smoke_Test' -> 'projects › kb_mcp › Smoke_Test'. The folder hierarchy
    is semantic signal for the model (where a memory lives implies what it's about); only
    the dot separators are humanized — segment slugs are left as-is. Empty/None -> ''
    (a guard; enrichable notes always carry a path)."""
    return (path or "").replace(".", " › ")


def sanitize_slug(raw: str) -> str:
    """Reduce an arbitrary name to a valid ltree label [A-Za-z0-9_]."""
    cleaned = _BAD_LABEL_CHARS.sub("_", raw.strip()).strip("_")
    if not cleaned:
        raise ValueError(f"slug is empty after sanitization: {raw!r}")
    return cleaned
