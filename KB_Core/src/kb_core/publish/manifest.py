"""Allowlist manifest for the public mirror (docs/publishing.md).

A path exports iff it matches at least one include pattern and no exclude
pattern. Patterns use fnmatch semantics where `*` crosses `/`, so `X/**`
matches any depth under X. Anything unmatched is private by default.

Media files (screenshots, PDFs, video) are a second, stricter allowlist: the
denylist only scans text, so an exported image could show real corpus content
no pattern will ever match. Every selected media path must also match an
`[export] allow_media` pattern, or the guard and kb-publish refuse.

Any OTHER file that is not strict UTF-8 must match `[export] allow_binary`
as well as be hash-pinned in the lockfile: the manifest is a gate file,
so letting binary bytes out needs --ack-gate-changes, not just an approval.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

# SVG is deliberately absent: it is text, so the denylist scan already reads it.
MEDIA_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico",
    ".heic", ".avif", ".pdf", ".mp4", ".mov", ".webm",
})

# Forms the text scan cannot read at all, refused outright — NOT allowlistable
# via allow_media, because there is no way to review what's inside from a diff
# (a .zip/.docx/.xlsx/.gz holding a denylisted name scans clean as raw bytes).
_FORBIDDEN_SUFFIXES = {
    **dict.fromkeys((".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".zst", ".7z",
                     ".rar", ".jar", ".whl", ".egg", ".lz4"), "archive"),
    ".ipynb": "notebook",   # JSON, but outputs embed base64 images and live query results
    **dict.fromkeys((".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt",
                     ".ods", ".odp", ".rtf", ".pages", ".numbers", ".key"),
                    "office document"),
    **dict.fromkeys((".sqlite", ".sqlite3", ".db", ".dump"), "database"),
    **dict.fromkeys((".pkl", ".pickle", ".parquet", ".feather", ".npy", ".npz",
                     ".h5", ".hdf5", ".arrow"), "serialized data"),
    # More compressed / mail-store / disk-image / model-weight forms.
    **dict.fromkeys((".svgz", ".br", ".zlib", ".lzma", ".z", ".cab", ".dmg", ".iso",
                     ".epub"), "archive"),
    **dict.fromkeys((".msg", ".pst", ".ost", ".olm", ".mbox", ".eml", ".emlx"),
                    "mail store"),
    **dict.fromkeys((".pt", ".pth", ".onnx", ".safetensors", ".joblib", ".bson",
                     ".duckdb", ".ckpt"), "serialized data"),
}


def forbidden_suffix(path: str) -> str | None:
    """The kind of unreviewable container `path` is (by suffix), or None."""
    return _FORBIDDEN_SUFFIXES.get(Path(path).suffix.lower())


@dataclass(frozen=True)
class Manifest:
    include: list[str]
    exclude: list[str]
    remote: str
    allow_media: tuple[str, ...] = ()
    allow_binary: tuple[str, ...] = ()


class ManifestError(ValueError):
    """The manifest can't be trusted to mean what it says — refuse, never guess."""


_ALLOWED_KEYS = {
    "publish": frozenset({"remote"}),
    "export": frozenset({"include", "exclude", "allow_media", "allow_binary"}),
}


def _patterns(table: dict, key: str) -> list[str]:
    raw = table.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise ManifestError(f"[export] {key} must be a list of strings")
    for pat in raw:
        # Each of these can never match a repo-relative git path under
        # fnmatchcase, so the pattern is a silent no-op — and an exclude that
        # excludes nothing fails OPEN.
        if not pat:
            raise ManifestError(f"[export] {key}: empty pattern")
        if pat.startswith("./"):
            raise ManifestError(f"[export] {key}: {pat!r} starts with './' (paths are repo-relative)")
        if pat.startswith("/"):
            raise ManifestError(f"[export] {key}: {pat!r} is absolute (paths are repo-relative)")
        if "\\" in pat:
            raise ManifestError(f"[export] {key}: {pat!r} contains a backslash (use '/')")
        if ".." in pat.split("/"):
            raise ManifestError(f"[export] {key}: {pat!r} contains a '..' segment")
    return raw


def load_manifest(path: Path) -> Manifest:
    """Parse and VALIDATE the manifest. Raises ManifestError on anything odd.

    Validation is strict because every failure mode of a lax parser is a fail-
    open: an unknown key (`excludes =`) is ignored, a './'-prefixed or
    case-variant pattern never matches, and the path it meant to hold back ships.
    """
    data = tomllib.loads(path.read_text())
    for table in data:
        if table not in _ALLOWED_KEYS:
            raise ManifestError(f"unknown table [{table}] in {path.name}")
    for table, allowed in _ALLOWED_KEYS.items():
        if not isinstance(data.get(table), dict):
            raise ManifestError(f"missing table [{table}] in {path.name}")
        unknown = sorted(set(data[table]) - allowed)
        if unknown:
            raise ManifestError(f"unknown key(s) in [{table}]: {', '.join(unknown)}")
    export = data["export"]
    include = _patterns(export, "include")
    exclude = _patterns(export, "exclude")
    allow_media = _patterns(export, "allow_media")
    allow_binary = _patterns(export, "allow_binary")
    if not include:
        raise ManifestError("[export] include is empty — nothing could ever export")
    seen: dict[str, str] = {}
    for pat in [*include, *exclude, *allow_media, *allow_binary]:
        prior = seen.setdefault(pat.lower(), pat)
        if prior != pat:
            raise ManifestError(
                f"case-variant duplicate patterns {prior!r} and {pat!r} — matching is "
                "case-sensitive, so one of them is a typo that matches nothing")
    remote = data["publish"].get("remote")
    if not isinstance(remote, str):
        raise ManifestError("[publish] remote must be a string")
    return Manifest(include=list(include), exclude=list(exclude), remote=remote,
                    allow_media=tuple(allow_media), allow_binary=tuple(allow_binary))


def unapproved_media(paths: list[str], m: Manifest) -> list[str]:
    """Selected paths that are media files not named by `allow_media`."""
    return [
        p for p in paths
        if Path(p).suffix.lower() in MEDIA_SUFFIXES and not _matches(p, list(m.allow_media))
    ]


def binary_allowed(path: str, allow_binary: tuple[str, ...] | list[str]) -> bool:
    return _matches(path, list(allow_binary))


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(path, p) for p in patterns)


# --- private-by-construction ---------------------------------------------
# Matched case-insensitively: macOS filesystems are case-insensitive, so
# `Private/` is the same directory as `private/` to the person who made it.
_DENY_SEGMENTS = frozenset({
    "private", "scratch", "tmp", "test-results", "playwright-report", ".cache",
})
# The only dot-directories that may ever export, as ROOT-anchored prefixes.
_ALLOWED_DOT_PREFIXES = (".github/", ".claude/skills/setup-kb/")
# The one workflow that runs on both repos. Every other workflow (notably the
# private-only publish-candidate.yml, which names the guard's secrets) stays home.
_PUBLIC_WORKFLOWS = frozenset({".github/workflows/ci.yml"})
# Dependabot config stays private: on a mirror only the publisher writes to, its
# branches cannot be created and its PRs could never merge; dependency updates
# land in the private repo and reach the mirror with the next sync.
_PRIVATE_GITHUB_FILES = frozenset({".github/dependabot.yml", ".github/dependabot.yaml"})
_DENY_SUFFIXES = (".dump", ".sqlite", ".sqlite3", ".db")
_DENY_NAMES = frozenset({".coverage", "agents.md", ".cursorrules", "publish.deny.toml"})
_DENY_EXACT = frozenset({"private/publish-state.json"})
# The names private material actually gets, not just the canonical ones.
# Directory segments, exact:
_DENY_DIR_SEGMENTS = frozenset({"secrets", "temp", "scratchpad"})
# A token (split on . _ -) of ANY segment, file names included:
# `private_notes/`, `notes-PRIVATE.md`, `my-scratch-dir/`.
_DENY_TOKENS = frozenset({"private", "scratch", "scratchpad"})
_SECRET_NAMES = frozenset({".env", ".envrc", ".npmrc", ".pypirc", ".netrc"})
_SECRET_SUFFIXES = (".pem", ".p12", ".pfx", ".jks", ".keystore")
_SSH_KEY = re.compile(r"id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?(?:\.pub)?")
_TOKEN_SPLIT = re.compile(r"[._-]")


def hard_deny_reason(path: str) -> str | None:
    """Why `path` can never be exported, or None if the manifest may decide.

    Pure function of the path string — no manifest input — so no edit to any
    TOML can turn it off. Kept deliberately small and literal: every rule here
    is one a reviewer can check at a glance.
    """
    if path in _DENY_EXACT:
        return "private publish state"
    parts = path.split("/")
    lowered = [s.lower() for s in parts]
    for seg in lowered:
        if seg in _DENY_SEGMENTS:
            return f"private-by-construction segment '{seg}/'"
    if any(s.startswith(".") for s in parts[:-1]) and not path.startswith(_ALLOWED_DOT_PREFIXES):
        return "dot-dir (only .github/ and .claude/skills/setup-kb/ may export)"
    if path.startswith(".github/workflows/") and path not in _PUBLIC_WORKFLOWS:
        return "workflow other than ci.yml (private-only)"
    if path.lower() in _PRIVATE_GITHUB_FILES:
        return "dependabot config (private-only: updates land upstream, then sync)"
    for seg in lowered:
        # `*.local.*` / `*.private.*`, extended to whole segments and dotfiles
        # (`prompts.local/`, `.env.local`): any dot-separated component after
        # the first is `local` or `private`.
        if any(c in ("local", "private") for c in seg.split(".")[1:]):
            return f"local/private overlay name '{seg}'"
    for seg in lowered[:-1]:
        if seg in _DENY_DIR_SEGMENTS:
            return f"private-by-construction segment '{seg}/'"
    for seg in lowered:
        if _DENY_TOKENS.intersection(_TOKEN_SPLIT.split(seg)):
            return f"private/scratch name '{seg}'"
    name = lowered[-1]
    if name in _DENY_NAMES:
        return f"private name '{parts[-1]}'"
    if (name in _SECRET_NAMES or (name.startswith(".env.") and not name.endswith(".example"))
            or name.endswith(_SECRET_SUFFIXES) or _SSH_KEY.fullmatch(name)):
        return f"secret / credential file '{parts[-1]}'"
    if name.startswith(".coverage."):
        return f"tool output '{parts[-1]}'"
    if name.endswith(_DENY_SUFFIXES):
        return f"database/dump file '{parts[-1]}'"
    if name.startswith("claude") and name.endswith(".md"):
        return f"agent-policy doc '{parts[-1]}'"
    return None


def select(paths: list[str], m: Manifest) -> list[str]:
    return [
        p for p in paths
        if _matches(p, m.include) and not _matches(p, m.exclude)
        and hard_deny_reason(p) is None
    ]
