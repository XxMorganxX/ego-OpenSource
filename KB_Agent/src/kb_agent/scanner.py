"""Walk the vault the way ObsidianAdapter does and stamp ids the way it does."""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kb_core.adapters.obsidian.stamp import current_id, stamp_file

log = logging.getLogger("kb-agent")


@dataclass(frozen=True)
class Observed:
    rel_path: str
    external_id: str
    text: str
    sha: str
    mtime_iso: str
    birthtime_iso: str | None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _fold(s: str) -> str:
    """NFC then lower-case: APFS hands back names in whatever normalisation they were created
    with, so a composed pattern must match a decomposed name (and vice versa)."""
    return unicodedata.normalize("NFC", s).lower()


def normalise_excludes(patterns: list[str]) -> list[str]:
    """Fold each pattern (`_fold`); strip leading `./` and `/` and a trailing `/` or `/**` (as
    suffixes). Patterns are matched against the path AND every directory prefix of it, so a
    pattern with no glob characters (`Private`) excludes itself and everything under it."""
    out: list[str] = []
    for p in patterns:
        p = _fold(p.strip())
        while p.startswith("./") or p.startswith("/"):
            p = p[2:] if p.startswith("./") else p[1:]
        while p.endswith("/**") or p.endswith("/"):
            p = p[:-3] if p.endswith("/**") else p[:-1]
        if p:
            out.append(p)
            if p.startswith("**/"):
                out.append(p[3:])                               # `**/x` also means a top-level x
    return out


def _excluded(rel: str, patterns: list[str]) -> bool:
    """Case-insensitive (APFS is), and over-exclusion is the safe direction for a privacy control."""
    if not patterns:
        return False
    parts = _fold(rel).split("/")
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return any(fnmatch.fnmatchcase(c, p) for c in candidates for p in patterns)


@dataclass(frozen=True)
class ScanError:
    rel_path: str          # vault-relative; "" is the vault root
    is_dir: bool
    reason: str            # an exception class / OS reason only, never note text


class Scan(list):
    """The notes a scan observed (a plain list of Observed) plus what it could NOT observe.

    A failed path, or anything under a failed directory, was not seen, which is not the same as
    gone: the pusher keeps its previous state (no delete, no re-send) and withholds the manifest.
    """

    def __init__(self, notes=(), errors=()) -> None:
        super().__init__(notes)
        self.errors: list[ScanError] = list(errors)

    def failed(self, rel: str) -> bool:
        for e in self.errors:
            if e.is_dir:
                if e.rel_path == "" or rel == e.rel_path or rel.startswith(e.rel_path + "/"):
                    return True
            elif rel == e.rel_path:
                return True
        return False


def _reason(exc: BaseException) -> str:
    # YAML errors quote the offending line, so only an OS reason or the class name is safe to log.
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return type(exc).__name__


def scan_vault(vault: Path, exclude: list[str]) -> Scan:
    out = Scan()

    def _fail(rel: str, is_dir: bool, exc: BaseException) -> None:
        err = ScanError(rel, is_dir, _reason(exc))
        out.errors.append(err)
        log.warning("scan could not read %s %s: %s", "directory" if is_dir else "note", rel or "<vault root>", err.reason)

    def _onerror(exc: OSError) -> None:
        # os.walk swallows scandir failures by default, so an unlistable directory would look
        # exactly like an emptied one (cf. ObsidianAdapter._iter_markdown).
        rel = os.path.relpath(exc.filename, vault) if getattr(exc, "filename", None) else "."
        _fail("" if rel == "." or rel.startswith("..") else Path(rel).as_posix(), True, exc)

    patterns = normalise_excludes(exclude)
    for root, dirs, files in os.walk(vault, onerror=_onerror):          # followlinks=False
        base = Path(root)
        # Symlinks are never followed: a link could carry an excluded note (or a file outside
        # the vault, which stamping would then modify) past the exclude list.
        dirs[:] = [d for d in dirs if not d.startswith(".") and not os.path.islink(base / d)
                   and not _excluded((base / d).relative_to(vault).as_posix(), patterns)]
        for name in files:
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = base / name
            rel = path.relative_to(vault).as_posix()
            if os.path.islink(path) or _excluded(rel, patterns):
                continue
            try:
                ext = stamp_file(path, rel)                     # writes id: when absent
                text = path.read_text(encoding="utf-8")
                if current_id(text) != ext:                     # a race re-stripped it: re-stamp
                    ext = stamp_file(path, rel); text = path.read_text(encoding="utf-8")
                st = path.stat()
            except Exception as exc:                            # one bad file never aborts a scan
                _fail(rel, False, exc)
                continue
            birth = getattr(st, "st_birthtime", None)
            out.append(Observed(rel_path=rel, external_id=ext, text=text,
                                sha=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                mtime_iso=_iso(st.st_mtime), birthtime_iso=_iso(birth) if birth else None))
    return out
