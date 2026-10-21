"""Personal-data scanner: layers 2 and 3 of the mirror safety model.

Patterns come from publish.deny.toml at the repo root. That file is NEVER
exported — its patterns are the identifiers being concealed.

`[[allow]]` entries in the same file are the one exception, and a narrow one:
an exact, case-sensitive literal (never a regex) that may appear in content
under listed path globs although it contains a denied term — the public repo's
own URL in its README, say. `scan_text(..., allows=...)` drops a deny match
ONLY when the match lies entirely inside a boundary-checked occurrence of an
allowed literal located in the very text (or decoded reading) being scanned.
A match that straddles a literal's edge — a denied term that starts in the
literal's last letters and runs on into whatever follows it, or ends in its
first letters — still hits, and so does every other occurrence of the denied
term (same line, same file), every case / homoglyph / invisible-character
variant and every out-of-scope path. Nothing is blanked before scanning:
blanking first broke such straddling terms apart and hid them. Path names,
commit metadata and canaries never get the exception.
The rules that keep an entry narrow are hard-coded here (`load_allows`), not
configurable: at most MAX_ALLOWS entries, each a printable-ASCII literal of at
least MIN_ALLOW_LEN characters with MIN_ALLOW_CONTEXT non-denied characters
around its denied part, scoped by globs that pin a directory or a file name.
What may follow an occurrence depends on its shape: a URL-shaped literal
(one containing '/') may be followed by a path, query or fragment but never by
a '.'/'..' segment (traversal to a sibling repo) or a dotted name; any other
literal only by the end, whitespace, or closing punctuation then whitespace.
"""
from __future__ import annotations

import re
import tomllib
import urllib.parse
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path


@dataclass(frozen=True)
class Hit:
    path: str
    line_no: int
    pattern: str
    line: str


class DenyConfigError(ValueError):
    """publish.deny.toml can't be trusted to mean what it says — refuse."""


@dataclass(frozen=True)
class Allow:
    text: str                   # exact, case-sensitive literal
    paths: tuple[str, ...]      # fnmatch globs (`*` crosses `/`), repo-relative
    reason: str


# Hard limits: raising any of them is a change to kb_core/publish (a gate file).
MAX_ALLOWS = 3                  # public repo URL, README maintainer credit, NOTICE copyright holder
MIN_ALLOW_LEN = 20
MIN_ALLOW_CONTEXT = 12          # non-space characters of the literal no deny pattern covers
_ALLOW_KEYS = frozenset({"text", "paths", "reason"})
# What may sit immediately before an allowed literal (besides whitespace and the
# start of the text). A URL may also carry one of _SCHEMES first.
_LEFT_BOUNDARY = frozenset("(<[\"'`*>")
_SCHEMES = ("git+https://", "https://")
_WORDISH = re.compile(r"[A-Za-z0-9_-]")
# What may close a sentence right after a literal ("... see URL)." / "*credit*").
_CLOSERS = frozenset(")>]\"'`*")
# After a literal that is not URL-shaped: these, then whitespace or the end.
_PROSE_TAIL = frozenset(".,;:!)*")
MASK = "#"


def load_patterns(path: Path) -> list[re.Pattern]:
    data = tomllib.loads(path.read_text())
    return [re.compile(p, re.IGNORECASE) for p in data["deny"]["patterns"]]


def _glob_problem(g: object) -> str | None:
    if not isinstance(g, str) or not g:
        return "must be a non-empty string"
    if not g.isascii() or not g.isprintable():
        return "must be printable ASCII"
    if g.startswith(("/", "./")) or "\\" in g or ".." in g.split("/"):
        return "must be a plain repo-relative path"
    if "[" in g or "]" in g:
        return "may not use character classes"
    wild = [i for i, c in enumerate(g) if c in "*?"]
    if wild:
        # `*` crosses `/` here (manifest semantics), so a glob must pin a
        # directory before its first wildcard or a file name after its last.
        if "/" not in g[:wild[0]] and "/" not in g[wild[-1]:]:
            return "must pin a directory ('docs/*.md') or a file name ('*/README.md')"
    return None


def _context_chars(text: str, patterns: list[re.Pattern]) -> tuple[int, bool]:
    """(non-space chars no deny pattern covers, whether any pattern matched)."""
    covered = [False] * len(text)
    matched = False
    for p in patterns:
        for m in p.finditer(text):
            matched = matched or m.end() > m.start()
            for i in range(m.start(), m.end()):
                covered[i] = True
    return sum(1 for i, c in enumerate(text) if not covered[i] and not c.isspace()), matched


def load_allows(path: Path, patterns: list[re.Pattern]) -> tuple[Allow, ...]:
    """Parse and VALIDATE the `[[allow]]` entries. Raises DenyConfigError.

    `patterns` are the file's deny patterns: an entry must contain a denied
    term (else it excuses nothing) and enough context around it that it can
    never be just the term.
    """
    data = tomllib.loads(path.read_text())
    raw = data.get("allow", [])
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise DenyConfigError("allow must be an array of tables ([[allow]])")
    if len(raw) > MAX_ALLOWS:
        raise DenyConfigError(f"at most {MAX_ALLOWS} [[allow]] entries (found {len(raw)}); "
                              "the cap is in kb_core/publish/deny.py")
    out: list[Allow] = []
    for n, e in enumerate(raw, 1):
        where = f"[[allow]] #{n}"
        unknown = sorted(set(e) - _ALLOW_KEYS)
        if unknown:
            raise DenyConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")
        text = e.get("text")
        if not isinstance(text, str) or not text:
            raise DenyConfigError(f"{where}: text must be a non-empty string")
        if not (text.isascii() and text.isprintable()):
            raise DenyConfigError(f"{where}: text must be printable ASCII")
        if text != text.strip() or "  " in text:
            raise DenyConfigError(f"{where}: text may not have leading, trailing or "
                                  "doubled whitespace")
        if len(text) < MIN_ALLOW_LEN:
            raise DenyConfigError(f"{where}: text is too short (< {MIN_ALLOW_LEN} chars) — "
                                  "an allow can never be a bare denied term")
        context, matched = _context_chars(text, patterns)
        if not matched:
            raise DenyConfigError(f"{where}: text matches no deny pattern, so it excuses "
                                  "nothing — remove it")
        if context < MIN_ALLOW_CONTEXT:
            raise DenyConfigError(f"{where}: too little context around the denied term "
                                  f"({context} < {MIN_ALLOW_CONTEXT} non-denied chars)")
        paths = e.get("paths")
        if not isinstance(paths, list) or not paths:
            raise DenyConfigError(f"{where}: paths must be a non-empty list of globs")
        for g in paths:
            if problem := _glob_problem(g):
                raise DenyConfigError(f"{where}: paths entry {g!r} {problem}")
        reason = e.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DenyConfigError(f"{where}: reason must be a non-empty string")
        if any(a.text == text for a in out):
            raise DenyConfigError(f"{where}: duplicate text")
        out.append(Allow(text, tuple(paths), reason))
    return tuple(out)


def _left_ok(text: str, i: int) -> bool:
    for scheme in _SCHEMES:
        if i >= len(scheme) and text.startswith(scheme, i - len(scheme)):
            i -= len(scheme)
            break
    if i == 0:
        return True
    c = text[i - 1]
    return c.isspace() or c in _LEFT_BOUNDARY


def _right_ok(text: str, j: int, *, git_suffix: bool = True) -> bool:
    """URL-shaped literal: may anything legitimately follow at `j`?"""
    c = text[j:j + 1]
    if not c or c.isspace():
        return True
    if not c.isascii() or _WORDISH.fullmatch(c):
        return False             # a longer name, or an invisible/look-alike glued on
    if c != ".":
        return True
    d = text[j + 1:j + 2]
    if not d or d.isspace() or d in _CLOSERS:
        return True              # sentence-ending period
    if git_suffix and text.startswith(".git", j):
        return _right_ok(text, j + 4, git_suffix=False)
    return False                 # `.private`, `..evil`, `./x`: a different (dotted) name


def _dot_segment_after(text: str, j: int) -> bool:
    """Does the URL path running on from `j` (to the next whitespace) hold a '.'
    or '..' segment — even percent-encoded, double-encoded or behind '\\'?
    `…/repo/../other` resolves to a sibling repo under the same owner."""
    k = j
    while k < len(text) and not text[k].isspace():
        k += 1
    path = re.split(r"[?#]", text[j:k], maxsplit=1)[0]
    for _ in range(3):           # %252e -> %2e -> .
        decoded = urllib.parse.unquote(path)
        if decoded == path:
            break
        path = decoded
    segments = path.replace("\\", "/").split("/")[1:]
    return any(s.rstrip("".join(_CLOSERS)) in (".", "..") for s in segments)


def _prose_right_ok(text: str, j: int) -> bool:
    """Non-URL literal (a credit line): end, whitespace, or closing punctuation
    followed by end/whitespace — never `/ @ - _ .x` or a letter/digit."""
    k = j
    while k < len(text) and text[k] in _PROSE_TAIL:
        k += 1
    return k == len(text) or text[k].isspace()


def _spans(text: str, literal: str, bounded: bool = True) -> list[tuple[int, int]]:
    url_shaped = "/" in literal
    out, start = [], 0
    while (i := text.find(literal, start)) != -1:
        j = i + len(literal)
        start = i + 1
        if not bounded:
            out.append((i, j))
            continue
        if url_shaped:
            right = _right_ok(text, j) and not _dot_segment_after(text, j)
        else:
            right = _prose_right_ok(text, j)
        if _left_ok(text, i) and right:
            out.append((i, j))
    return out


def allowed_spans(text: str, allows: tuple[Allow, ...] | list[Allow], *,
                  bounded: bool = True) -> list[tuple[int, int]]:
    """Every exact occurrence of each literal in `text` — boundary-checked
    unless `bounded` is False (see scan_text)."""
    return [s for a in allows for s in _spans(text, a.text, bounded)]


def applicable(rel_path: str, allows: tuple[Allow, ...] | list[Allow]) -> list[Allow]:
    """The entries whose path globs match `rel_path` (case-sensitive)."""
    return [a for a in allows if any(fnmatchcase(rel_path, g) for g in a.paths)]


def mask_text(text: str, allows: tuple[Allow, ...] | list[Allow]) -> str:
    """`text` with every allowed occurrence replaced by MASK characters of the
    same length. NOT a way to apply the allow (blanking first breaks a denied
    term that straddles the literal's edge apart — use scan_text's `allows`);
    structural only uses it to keep refusing decoded readings that turn a
    variant (homoglyph, invisible character, entity) into the exact literal."""
    spans = allowed_spans(text, allows)
    if not spans:
        return text
    chars = list(text)
    for i, j in spans:
        chars[i:j] = MASK * (j - i)
    return "".join(chars)


def _unexcused(pat: re.Pattern, line: str, spans: list[tuple[int, int]]) -> bool:
    """Is there a match of `pat` in `line` NOT entirely inside one of `spans`?

    Every start position is tried (pos = previous start + 1), so a contained
    match never hides an overlapping one that spills out of the literal. At a
    start whose leftmost-first match is contained, every longer end past the
    literal is also tried (fullmatch), so an alternation whose short form fits
    inside the literal cannot hide a long form that runs past its edge."""
    if not spans:
        return pat.search(line) is not None
    pos = 0
    while pos <= len(line):
        m = pat.search(line, pos)
        if m is None:
            return False
        inside = [j for i, j in spans if i <= m.start() and m.end() <= j]
        if not inside:
            return True
        edge = min(inside)
        for end in range(edge + 1, min(len(line), edge + _SPILL_MAX) + 1):
            if pat.fullmatch(line, m.start(), end):
                return True
        pos = m.start() + 1
    return False


# How far past an allowed literal's edge _unexcused looks for a longer match
# starting inside it. Bounded so a long line costs at most this many fullmatch
# calls per contained match; deny terms are names and hosts, far shorter.
_SPILL_MAX = 512


def scan_text(rel_path: str, text: str, patterns: list[re.Pattern], *,
              allows: tuple[Allow, ...] | list[Allow] = (),
              bounded: bool = True) -> list[Hit]:
    """One Hit per (line, pattern) that matches.

    allows: [[allow]] entries to honour, ALREADY scoped by the caller
    (`applicable`). A match is dropped only if it lies entirely inside an
    allowed occurrence found in this same line; one that straddles an
    occurrence's edge, or lies outside it, still hits.

    bounded=False drops the boundary rules when locating occurrences — for a
    DECODED reading only (structural.scan_blob), where decoding garbles the
    literal's neighbours (UTF-7 reads `git+https:` as a shift sequence) and a
    match wholly inside a copy of the literal reveals nothing the literal
    doesn't. The text a reader sees is always scanned bounded."""
    hits: list[Hit] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        spans = allowed_spans(line, allows, bounded=bounded) if allows else []
        for pat in patterns:
            if _unexcused(pat, line, spans):
                hits.append(Hit(rel_path, line_no, pat.pattern, line.strip()[:200]))
    return hits


def scan_allowed(rel_path: str, text: str, patterns: list[re.Pattern],
                 allows: tuple[Allow, ...] | list[Allow], *,
                 scope: str | None = None) -> list[Hit]:
    """scan_text honouring only the entries scoped to `scope` (default
    `rel_path`) — the one way every content gate applies [[allow]]."""
    own = applicable(scope if scope is not None else rel_path, allows) if allows else []
    return scan_text(rel_path, text, patterns, allows=own)


def scan_file(root: Path, rel_path: str, patterns: list[re.Pattern]) -> list[Hit]:
    # errors="replace" so identifier strings inside binary blobs still match.
    text = (root / rel_path).read_bytes().decode("utf-8", errors="replace")
    return scan_text(rel_path, text, patterns)
