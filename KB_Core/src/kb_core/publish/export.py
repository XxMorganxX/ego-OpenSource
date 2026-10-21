"""Assemble the public export tree from a committed ref.

The tree is built from the COMMITTED blobs (`git ls-tree` + `git cat-file
--batch`), not `git archive`. `git archive` applies gitattributes —
export-subst, export-ignore, eol/text conversion, filters — taken from the
archived tree and from untracked sources ($GIT_DIR/info/attributes,
core.attributesFile, the system file). Any of those can make the published
bytes differ from what was reviewed, approved and hash-pinned in the lockfile,
and the untracked ones never show up in a diff. Reading blobs is attribute-free
by construction; git still runs with no global/system config and no attribute
files as a second wall.

Symlinks (mode 120000) and gitlinks (160000) are filtered out of the listing
BEFORE anything is written: an absolute symlink used to abort the tar
extraction, so one stray link anywhere in the repo blocked every publish.
They are still never exported.

Selection happens on the path LIST, before any blob is written: the manifest
is read from the ref as a blob, and only the selected files ever touch dest.
Writing the whole tree first and pruning after left the entire private tree
in dest whenever the process died between the two (SIGKILL, OOM, a runner
timeout), where no Python cleanup runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unicodedata
from collections import defaultdict
from pathlib import Path

from kb_core.publish import manifest as manifest_mod

# git's export-subst placeholder, assembled so this file (exported itself)
# never contains it. A selected file holding it is refused: the public repo,
# or anyone running `git archive` on it with export-subst set, would get
# commit metadata substituted into published bytes nobody reviewed.
FORMAT_PLACEHOLDER = b"$" + b"Format:"

_REGULAR = {"100644": 0o644, "100755": 0o755}
_SKIPPED = {"120000": "symlink", "160000": "gitlink (submodule)"}


class ExportError(RuntimeError):
    pass


def _git_env() -> dict:
    """No inherited GIT_* (GIT_DIR, GIT_INDEX_FILE, GIT_CONFIG_PARAMETERS could
    point the read at another repo or reconfigure it), no global or system
    config, no system attributes."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_ATTR_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


def _git(root: Path, args: list[str], *, input: bytes | None = None) -> bytes:
    try:
        return subprocess.run(
            ["git", "-c", f"core.attributesFile={os.devnull}", *args],
            cwd=root, check=True, capture_output=True, input=input, env=_git_env(),
        ).stdout
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise ExportError(f"git {args[0]} failed: {err}") from e


def _safe_rel(rel: str) -> bool:
    parts = rel.split("/")
    return all(p not in ("", ".", "..") and p.lower() != ".git" for p in parts) \
        and "\0" not in rel


def _list_tree(root: Path, ref: str) -> list[tuple[str, str, str]]:
    """(mode, blob id, path) for every regular file at ref; links are reported
    and dropped here, before anything touches the filesystem."""
    out = _git(root, ["ls-tree", "-r", "-z", "--full-tree", f"{ref}^{{tree}}"])
    entries = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _, raw_path = rec.partition(b"\t")
        mode, kind, oid = meta.decode().split()
        rel = raw_path.decode("utf-8", "surrogateescape")
        if mode in _SKIPPED:
            print(f"export: stripped {_SKIPPED[mode]} {rel}")
            continue
        if kind != "blob" or mode not in _REGULAR:
            raise ExportError(f"unexpected tree entry {mode} {kind} {rel!r} at {ref!r}")
        if not _safe_rel(rel):
            raise ExportError(f"unsafe path in tree at {ref!r}: {rel!r}")
        entries.append((mode, oid, rel))
    return entries


def _fold(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def _case_collisions(rels: list[str]) -> set[str]:
    """Paths that a case- or normalization-insensitive filesystem (macOS) would
    merge: same file name, or one directory spelled two ways. Written to disk,
    the twin overwrites the file or lands under the other spelling's directory —
    where an unselected `SRC/x` could be selected as `src/x`."""
    spellings: dict[str, set[str]] = defaultdict(set)
    for rel in rels:
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            spellings[_fold(prefix)].add(prefix)
    clashing = {sp for group in spellings.values() if len(group) > 1 for sp in group}
    if not clashing:
        return set()
    out = set()
    for rel in rels:
        parts = rel.split("/")
        if any("/".join(parts[:i]) in clashing for i in range(1, len(parts) + 1)):
            out.add(rel)
    return out


def _write_blobs(root: Path, dest: Path, entries: list[tuple[str, str, str]]) -> None:
    """Write each blob's exact bytes (one `cat-file --batch` for the lot)."""
    if not entries:
        return
    out = _git(root, ["cat-file", "--batch"],
               input="".join(f"{oid}\n" for _, oid, _ in entries).encode())
    pos = 0
    base = dest.resolve()
    for mode, oid, rel in entries:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode().split()
        if len(header) != 3 or header[0] != oid or header[1] != "blob":
            raise ExportError(f"cat-file returned {header!r} for {rel!r}")
        size = int(header[2])
        data = out[nl + 1: nl + 1 + size]
        pos = nl + 1 + size + 1                      # content + trailing LF
        target = dest / rel
        if not target.resolve().is_relative_to(base):
            raise ExportError(f"path escapes the export dir: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, _REGULAR[mode])


MANIFEST_REL = "publish.manifest.toml"


def _manifest_at(root: Path, ref: str, entries: list[tuple[str, str, str]]
                 ) -> manifest_mod.Manifest:
    """The manifest committed at ref, read as a blob — never from dest."""
    oid = next((o for _, o, rel in entries if rel == MANIFEST_REL), None)
    if oid is None:
        raise ExportError(f"{MANIFEST_REL} not present at ref {ref!r}")
    data = _git(root, ["cat-file", "blob", oid])
    with tempfile.TemporaryDirectory(prefix="kb-export-m-") as td:
        mp = Path(td) / MANIFEST_REL
        mp.write_bytes(data)
        try:
            return manifest_mod.load_manifest(mp)
        except manifest_mod.ManifestError as e:
            raise ExportError(f"{MANIFEST_REL} at ref {ref!r} is invalid: {e}") from e


def assemble(root: Path, ref: str, dest: Path) -> list[str]:
    """Write the committed blobs at ref into dest, filtered by manifest allowlist.

    Assembles a publishable tree by: listing the tree at ref, dropping symlinks
    and gitlinks, reading the manifest at that ref (as a blob), selecting on
    the path list, then writing only the selected blobs' exact bytes
    (executable bit kept). An unselected file is never written to dest.

    Args:
        root: git repo root
        ref: git ref (branch, tag, commit) to export
        dest: destination directory; must be empty or absent

    Returns:
        sorted list of relative paths (str) kept in dest

    Raises:
        ExportError if:
          - dest exists and is not empty (stale content would leak)
          - ref does not resolve, or the tree holds an unsafe path
          - publish.manifest.toml is missing or invalid at ref
          - manifest selects zero files
          - a selected file contains the export-subst placeholder

    On any failure, dest is removed entirely (fail-closed; no partial export).
    """
    # Refuse if dest exists and contains anything (stale content guard)
    if dest.exists():
        if any(dest.iterdir()):
            raise ExportError(f"dest {dest} is not empty — refusing (stale content would leak into the export)")

    try:
        dest.mkdir(parents=True, exist_ok=True)
        entries = _list_tree(root, ref)
        m = _manifest_at(root, ref, entries)

        # Case/normalization twins would merge on disk; neither is written.
        collided = _case_collisions([rel for _, _, rel in entries])
        clash = manifest_mod.select(sorted(collided), m)
        if clash:
            raise ExportError(
                "selected paths differ from other paths only by letter case or Unicode "
                "normalization (they would merge on a case-insensitive filesystem): "
                + ", ".join(sorted(clash)[:10]) + " — rename one")
        all_paths = sorted(rel for _, _, rel in entries if rel not in collided)
        keep = set(manifest_mod.select(all_paths, m))
        if not keep:
            raise ExportError("manifest selected zero files — refusing")
        _write_blobs(root, dest, [e for e in entries if e[2] in keep])
        subst = [rel for rel in sorted(keep)
                 if FORMAT_PLACEHOLDER in (dest / rel).read_bytes()]
        if subst:
            raise ExportError(
                "export-subst placeholder in exported file(s): " + ", ".join(subst[:10])
                + " — git archive would substitute commit metadata there; remove it")
        return sorted(keep)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
