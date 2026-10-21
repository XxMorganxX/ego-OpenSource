"""Structural detectors: refuse the SHAPE of private data, not just known names.

The denylist (deny.py) only catches identifiers someone already listed. These
rules catch what it structurally cannot:

- path names are deny-scanned too (a file NAMED after a person ships even if
  its body is clean), plain and in the folded reading (NFKC + homoglyphs,
  invisible characters stripped); a path name carrying an invisible, bidi or
  control character is refused outright;
- text must be strict UTF-8 with no NUL and no BOM — UTF-16 interleaves NULs
  through every identifier and slid past the errors='replace' line scan. A file
  that genuinely needs other bytes is exempt only when it matches the
  manifest's `[export] allow_binary` (a gate file, so --ack-gate-changes) AND the path
  lockfile hash-pins its exact bytes (the `is_pinned` hook = lock.pinned_hook,
  recorded by `kb-publish approve`). Allowed binaries are deny-scanned in their
  UTF-16 readings as well;
- archive / notebook / office / database / pickle suffixes are refused outright,
  and so is any file whose MAGIC BYTES say container (gzip, zip, 7z, xz, bz2,
  zstd, SQLite, pickle, parquet, npy, OLE2 ...) whatever it is named;
- obfuscated identifiers: every text file is ALSO deny-scanned in its
  decoded readings — NFKC + homoglyph-folded with invisible characters
  stripped, HTML-entity and %XX unescaped, string-literal fragments joined,
  UTF-7, and each base64 run decoded at offsets 0-3 (a stray character
  glued on misaligns it; a base64 gzip/zip is a container) —
  and invisible / bidi control characters are refused outright. The
  lockfile only gates NEW paths, so a payload appended to an approved file
  relies on exactly this;
- emails only on reserved domains, no real home directories, no routable IPv4,
  no phone numbers, no TEMP/revert markers in exported TOML (live config);
- `[[allow]]` literals (deny.py) excuse a deny match in file CONTENT only when
  the match lies entirely inside an exact allowed occurrence located in the
  same text or decoded reading; path names, UTF-16 readings and every
  structural rule never take them;
- a warn-level lint for phrasing that usually means "copied from the real box"
  (a US time-zone name, "live" + "corpus", "measured" + a year). Warnings never
  block.

The rule SOURCES here (and the fixtures in test_publish_structural.py) are
written so they don't match themselves — this file is exported and scanned
like any other; nothing is exempt by path.

Findings reuse `deny.Hit` so every caller prints and counts them one way.
`pattern` is "structural: <rule>" for refusals and "lint: <rule>" for warnings.
"""
from __future__ import annotations

import base64
import binascii
import html
import ipaddress
import re
import unicodedata
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kb_core.publish import deny, manifest

PinnedHook = Callable[[str, bytes], bool]


@dataclass
class Report:
    hits: list[deny.Hit] = field(default_factory=list)       # refusals
    warnings: list[deny.Hit] = field(default_factory=list)   # never block


# --- encoding --------------------------------------------------------------

_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")


def encoding_problem(data: bytes) -> str | None:
    if data.startswith(_BOMS):
        return "byte-order mark (BOM)"
    if b"\x00" in data:
        return "NUL byte (binary or UTF-16)"
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "not valid UTF-8"
    return None


# --- containers by content -------------------------------------------------
# A suffix is a claim; the bytes are the fact. Each entry: (kind, magic at
# offset 0). Text that merely starts with the same letters is not matched:
# every magic here contains bytes no UTF-8 text starts with, or is checked
# more strictly below.
_MAGIC: tuple[tuple[str, bytes], ...] = (
    ("gzip", b"\x1f\x8b"),
    ("zip", b"PK\x03\x04"), ("zip", b"PK\x05\x06"), ("zip", b"PK\x07\x08"),
    ("7z", b"7z\xbc\xaf\x27\x1c"),
    ("xz", b"\xfd7zXZ\x00"),
    ("zstd", b"\x28\xb5\x2f\xfd"),
    ("lz4", b"\x04\x22\x4d\x18"),
    ("rar", b"Rar!\x1a\x07"),
    ("SQLite database", b"SQLite format 3\x00"),
    ("numpy array", b"\x93NUMPY"),
    ("OLE2 (doc/xls/msg)", bytes.fromhex("d0cf11e0a1b11ae1")),
    ("PST mail archive", b"!BDN"),
    ("HDF5", b"\x89HDF\r\n\x1a\n"),
)


def container_magic(data: bytes) -> str | None:
    """The container type `data` is, judged by its bytes, else None."""
    for kind, magic in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:3] == b"BZh" and data[3:4].isdigit() and data[4:10] == b"1AY&SY":
        return "bzip2"
    if len(data) >= 2 and data[0] == 0x80 and 2 <= data[1] <= 5:
        return "pickle"
    if len(data) >= 8 and data[:4] == b"PAR1" and data[-4:] == b"PAR1":
        return "parquet"
    if len(data) >= 2 and data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA):
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return "zlib"
    return None


def utf16_readings(data: bytes) -> list[str]:
    """Every UTF-16 reading of `data` (both byte orders, both alignments)."""
    return [data[off:].decode(codec, errors="replace")
            for codec in ("utf-16-le", "utf-16-be") for off in (0, 1)]


# --- content rules ---------------------------------------------------------

_EMAIL = re.compile(
    r"(?<![\w.%+-])([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])")
_RESERVED_TLDS = frozenset({"example", "invalid", "test", "localhost"})


def _email_allowed(local: str, domain: str) -> bool:
    d = domain.lower()
    labels = d.split(".")
    if labels[-1] in _RESERVED_TLDS:             # RFC 2606/6761: *.example, *.invalid, *.test
        return True
    if labels[-2] == "example":                  # example.com/.org/.net and their subdomains
        return True
    if d == "users.noreply.github.com" or d.endswith(".users.noreply.github.com"):
        return True
    return local == "git"                        # git@host:owner/repo — an ssh remote


def _is_remote_userinfo(line: str, m: re.Match) -> bool:
    # `https://token@github.com/...` and scp-style `user@host:owner/repo` are
    # git-remote fixtures (credential-stripping tests), not addresses.
    token_start = max(line.rfind(" ", 0, m.start()), line.rfind("\t", 0, m.start())) + 1
    if "://" in line[token_start:m.start() + 1]:
        return True
    return re.match(r":[\w~./-]", line[m.end():]) is not None


# `/Users/<name>`: placeholders are fine, a real login name is not.
_USERS = re.compile(r"/Users/([^/\s'\"`)\]}>,;:]+)")
_PLACEHOLDER_USERS = frozenset({
    "you", "me", "example", "user", "username", "name", "shared", "your-name",
    "yourname", "your_name", "someone", "runner",
})
# A concrete path under Desktop reveals one person's layout; a bare mention
# ("TCC blocks ~/Desktop or ~/Documents") does not.
_DESKTOP = re.compile(r"(?:~|\$HOME|\$\{HOME\})/Desktop/\S")


def _user_allowed(name: str) -> bool:
    n = name.lower()
    return (n in _PLACEHOLDER_USERS or len(n) == 1
            or n[0] in "<$*{([")                     # <name>, $USER, ${USER}, globs, regexes


_IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\d)")
_ALLOWED_NETS = tuple(ipaddress.IPv4Network(n) for n in (
    "127.0.0.0/8",                                       # loopback
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",  # RFC 5737 documentation
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",      # RFC 1918 private (docker, LAN docs)
))


def _ip_allowed(text: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(text)
    except ValueError:
        return True                                      # 999.1.2.3 isn't an address
    if text == "0.0.0.0" or text.startswith("255."):     # bind-all, netmasks
        return True
    return any(ip in net for net in _ALLOWED_NETS)


# NANP shapes only, deliberately narrow; 555-01xx is the reserved fictional block.
_PHONE = re.compile(
    r"(?<![\w.+/-])(?:\+?1[-. ])?\(?[2-9]\d{2}\)?[-. ]([2-9]\d{2})[-. ](\d{4})(?![\w-])")

_TOML_MARKER = re.compile(r"\bTEMP\b|(?i:\brevert|\btemp\b|\btemporary\b)")   # any case: `Temp`, `REVERT`

_LINT = (
    ("lint: tz-name", re.compile(r"Americ[a]/")),
    ("lint: live-corpus", re.compile(r"live[ ]corpus", re.IGNORECASE)),
    ("lint: measured-year", re.compile(r"measured[ ]20\d\d", re.IGNORECASE)),
)


# Zero-width, soft-hyphen, word-joiner, BOM-as-ZWNBSP and bidi controls: they
# hide characters inside an identifier, or reorder what a reviewer sees.
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064"
                        "\u2066-\u2069\ufeff]")

# Cyrillic and Greek letters that render like Latin ones. NFKC already folds
# fullwidth and mathematical alphanumerics; these it leaves alone.
_HOMOGLYPHS = str.maketrans(
    "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0456\u0458\u0455\u0501\u043a"
    "\u043c\u0442\u043d\u0432"
    "\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0423\u0425"
    "\u0406\u0408\u0405"
    "\u03b1\u03bf\u03c1\u03bd\u03b9\u03ba\u03c4\u03c5\u03c7\u03b5"
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4"
    "\u03a5\u03a7",
    "aeopcyxijsdkmthb"
    "ABEKMHOPCTYXIJS"
    "aopvikiuxe"
    "ABEZHIKMNOPTYX")
# Adjacent string literals (`"ab" + "cd"`, implicit `"ab" "cd"`), joined.
_FRAGMENT_JOIN = re.compile(r"""(["'`])\s*\+?\s*\1""")
_B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")   # base64("quincy") is 8 chars
_UTF7_SHIFT = re.compile(r"\+[A-Za-z0-9+/]{3,}-?")


def _b64_decode(run: str) -> bytes | None:
    s = run.rstrip("=").replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return None


def _b64_alignments(run: str) -> list[bytes]:
    """`run` decoded from offsets 0-3, each trimmed to a decodable length: a
    stray alphabet character glued on before (or after) an encoded identifier
    shifts every 4-character group and decodes it as garbage otherwise. At
    most four decodes per run, each no longer than the run."""
    out = []
    body = run.rstrip("=")
    for off in range(4):
        seg = body[off:]
        if len(seg) % 4 == 1:
            seg = seg[:-1]
        if len(seg) < 4:
            break
        raw = _b64_decode(seg)
        if raw:
            out.append(raw)
    return out


def folded(text: str) -> str:
    """NFKC + homoglyph-folded, invisible characters stripped."""
    return unicodedata.normalize("NFKC", _INVISIBLE.sub("", text)).translate(_HOMOGLYPHS)


# What a path name may never carry: the invisible/bidi set plus C0/C1 controls.
_PATH_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def decoded_readings(text: str) -> list[tuple[str, str]]:
    """(name, text) readings of `text` an obfuscated identifier would show up in."""
    out = [("folded", folded(text))]
    if "&" in text:
        out.append(("html-entity", html.unescape(text)))
    if "%" in text:
        out.append(("percent", urllib.parse.unquote(text)))
    if _FRAGMENT_JOIN.search(text):
        out.append(("joined-literal", _FRAGMENT_JOIN.sub("", text)))
    if _UTF7_SHIFT.search(text):
        out.append(("utf-7", text.encode("utf-8").decode("utf-7", errors="replace")))
    decoded = []
    for m in _B64_RUN.finditer(text):
        for raw in _b64_alignments(m.group()):
            decoded.append(raw.decode("utf-8", errors="replace"))
    if decoded:
        out.append(("base64", "\n".join(decoded)))
    return out


def _b64_containers(rel: str, text: str) -> list[deny.Hit]:
    hits = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for m in _B64_RUN.finditer(line):
            if len(m.group()) < 24:
                continue
            raw = _b64_decode(m.group()) or b""
            kind = ("gzip" if raw.startswith(b"\x1f\x8b\x08")
                    else "zip" if raw.startswith(b"PK\x03\x04") else None)
            if kind:
                hits.append(_hit(rel, line_no, f"structural: base64-encoded {kind}", line))
                break
    return hits


def _hit(rel: str, line_no: int, rule: str, line: str) -> deny.Hit:
    return deny.Hit(rel, line_no, rule, line.strip()[:200])


def scan_content(rel: str, text: str) -> list[deny.Hit]:
    """Structural refusals for one decoded file."""
    hits: list[deny.Hit] = []
    is_toml = rel.lower().endswith(".toml")
    for line_no, line in enumerate(text.splitlines(), 1):
        for m in _EMAIL.finditer(line):
            if not _email_allowed(m.group(1), m.group(2)) and not _is_remote_userinfo(line, m):
                hits.append(_hit(rel, line_no, "structural: email domain", line))
                break
        if (any(not _user_allowed(m.group(1)) for m in _USERS.finditer(line))
                or _DESKTOP.search(line)):
            hits.append(_hit(rel, line_no, "structural: home path", line))
        if any(not _ip_allowed(m.group(1)) for m in _IPV4.finditer(line)):
            hits.append(_hit(rel, line_no, "structural: IPv4 address", line))
        if any(not (m.group(1) == "555" and m.group(2).startswith("01"))
               for m in _PHONE.finditer(line)):
            hits.append(_hit(rel, line_no, "structural: phone number", line))
        if is_toml and _TOML_MARKER.search(line):
            hits.append(_hit(rel, line_no, "structural: TEMP/revert marker in TOML", line))
        if _INVISIBLE.search(line):
            hits.append(_hit(rel, line_no, "structural: invisible/bidi control character",
                             _INVISIBLE.sub("\u2423", line)))
    hits.extend(_b64_containers(rel, text))
    return hits


def lint_content(rel: str, text: str) -> list[deny.Hit]:
    out: list[deny.Hit] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for rule, pat in _LINT:
            if pat.search(line):
                out.append(_hit(rel, line_no, rule, line))
    return out


def scan_files(root: Path, paths: list[str], patterns: list[re.Pattern], *,
               is_pinned: PinnedHook | None = None,
               allow_binary: tuple[str, ...] = (),
               allows: tuple[deny.Allow, ...] = ()) -> Report:
    """Deny-scan + structural-scan every path (repo-relative, under `root`).

    Media files (manifest.MEDIA_SUFFIXES) are gated by allow_media elsewhere,
    so they skip the UTF-8 rule here but are still deny-scanned byte-wise.
    Any other non-UTF-8 file needs `allow_binary` (the manifest's) AND a pin.
    `allows`: the deny file's [[allow]] entries (see scan_blob).
    """
    rep = Report()
    for rel in paths:
        f = root / rel
        data = f.read_bytes() if f.is_file() else None
        scan_blob(rep, rel, data, patterns, is_pinned=is_pinned, allow_binary=allow_binary,
                  allows=allows)
    return rep


def scan_blob(rep: Report, rel: str, data: bytes | None, patterns: list[re.Pattern], *,
              is_pinned: PinnedHook | None = None, allow_binary: tuple[str, ...] = (),
              deny_only: bool = False, allows: tuple[deny.Allow, ...] = ()) -> None:
    """Scan one file's bytes into `rep`. data=None: the path alone (no such file).

    deny_only: identifiers only (plain, decoded and UTF-16 readings) — for blobs
    that were gated when they were published and are re-read against today's
    denylist (`kb-publish accept` over public history).

    allows: [[allow]] entries (deny.load_allows); only those scoped to `rel`
    apply. The original text and every decoded reading of it are scanned in
    full, and a match is dropped only if it lies entirely inside an allowed
    occurrence located in that same text/reading — so a denied term that a
    join / entity / escape glues onto the literal's edge still hits. In the
    text itself occurrences are boundary-checked; in a decoded reading any
    exact copy counts (decoding garbles neighbours: deny.scan_text). The
    readings of the text with its boundary-checked occurrences blanked are
    scanned WITHOUT the allow as well, so a variant that only a decoding turns
    into the exact literal (homoglyph, invisible character, entity) — or a
    boundary violation only a decoding produces — still hits: never weaker
    than blanking alone was.
    Path names, the UTF-16 readings and the structural rules never take it.
    """
    path_hits = deny.scan_text(rel, rel, patterns)
    rep.hits.extend(_hit(h.path, 0, f"path name: {h.pattern}", rel) for h in path_hits)
    # The name a reviewer reads is not the name that matched: fold it too.
    seen = {h.pattern for h in path_hits}
    for h in deny.scan_text(rel, folded(rel), patterns):
        if h.pattern not in seen:
            seen.add(h.pattern)
            rep.hits.append(_hit(rel, 0, f"path name: {h.pattern} [folded reading]", rel))
    if not deny_only and (_INVISIBLE.search(rel) or _PATH_CONTROL.search(rel)):
        shown = _PATH_CONTROL.sub("\u2423", _INVISIBLE.sub("\u2423", rel))
        rep.hits.append(_hit(shown, 0, "structural: invisible/bidi/control character in "
                                       "path name", shown))
    if not deny_only:
        kind = manifest.forbidden_suffix(rel)
        if kind:
            rep.hits.append(_hit(rel, 0, f"structural: {kind} file ({Path(rel).suffix})",
                                 "(container — not reviewable from a diff)"))
            return
    if data is None:
        return
    magic = container_magic(data)
    if magic and not deny_only:
        rep.hits.append(_hit(rel, 0, f"structural: {magic} container (by magic bytes)",
                             "(container — not reviewable from a diff, whatever its name)"))
        return
    is_media = Path(rel).suffix.lower() in manifest.MEDIA_SUFFIXES
    problem = None if is_media else encoding_problem(data)
    if deny_only:
        pass
    elif problem and not manifest.binary_allowed(rel, allow_binary):
        rep.hits.append(_hit(rel, 0, f"structural: {problem}",
                             "(not strict UTF-8 — only a path in the manifest's [export] "
                             "allow_binary, hash-pinned in the lockfile, may be)"))
    elif problem and not (is_pinned and is_pinned(rel, data)):
        rep.hits.append(_hit(rel, 0, f"structural: {problem}",
                             "(not strict UTF-8 — pin its hash in the lockfile if intended)"))
    # errors="replace" so identifiers inside binary blobs still match.
    text = data.decode("utf-8", errors="replace")
    own = deny.applicable(rel, allows) if allows else []
    rep.hits.extend(deny.scan_text(rel, text, patterns, allows=own))
    if problem or is_media or magic:
        # UTF-16 interleaves NULs through every identifier: read it as such.
        seen = {(h.pattern, h.line_no) for h in rep.hits if h.path == rel}
        for reading in utf16_readings(data):
            for h in deny.scan_text(rel, reading, patterns):
                if (h.pattern, h.line_no) not in seen:
                    seen.add((h.pattern, h.line_no))
                    rep.hits.append(h)
    if not is_media:
        # Obfuscated readings: one hit per pattern per file is enough
        # to refuse; a pattern the plain scan already found isn't repeated.
        found = {h.pattern for h in rep.hits if h.path == rel}
        readings = [(name, reading, own) for name, reading in decoded_readings(text)]
        if own:
            readings += [(name, reading, ()) for name, reading
                         in decoded_readings(deny.mask_text(text, own))]
        for name, reading, excuse in readings:
            for h in deny.scan_text(rel, reading, patterns, allows=excuse, bounded=False):
                if h.pattern not in found:
                    found.add(h.pattern)
                    rep.hits.append(deny.Hit(rel, h.line_no,
                                             f"{h.pattern} [{name} reading]", h.line))
        if not deny_only:
            rep.hits.extend(scan_content(rel, text))
            rep.warnings.extend(lint_content(rel, text))
