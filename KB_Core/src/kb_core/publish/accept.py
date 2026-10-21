"""`kb-publish accept`: scan what is ACTUALLY public, all of it.

The last step of every publish (docs/publishing.md).

Every gate before the push scans the export it built. Acceptance asks the
other question — what does the public repo hold now? — so it scans a fresh
read-only clone, not anything derived from the private repo:

- every file at public main, whatever the manifest selects (a commit made
  outside kb-publish can add a path no selection would), with the real deny
  file and every structural rule; a non-UTF-8 file passes only as the
  published sync commit's upstream lockfile pinned it and the public
  manifest's allow_binary allows it;
- every other blob in public history, against TODAY's denylist (identifiers
  added since a publish are still in history — this is how you find out);
- every commit and tag object, deny-scanned and structurally (email domains
  in identity lines and trailers).

Identity excuse (`--allow-identity-hit`, the same flag a push takes): a deny
hit is excused only on an author/committer/tagger HEADER line whose name and
email exactly equal the publish identity (KB_PUBLISH_IDENTITY_NAME/EMAIL).
Message bodies, trailers, other identities and file content never are, and
the structural detectors are never excused.

Only branches and tags are fetched (refs/heads/*, refs/tags/*): GitHub also
advertises refs/pull/* — contributors' PR heads and GitHub's merge commits,
written by people other than the publisher and not part of what kb-publish
published. A mirror clone pulled them in, so the first PR made acceptance
permanently red.

Read-only: the clone is a throwaway bare repo in a temp dir. Any hit refuses.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from kb_core.publish import deny, gate, git_ops, lock, manifest, structural

Clone = Callable[[str, Path], None]

# One git identity header line: `author Name <email> 1700000000 +0000`.
_IDENT_HEADER = re.compile(r"(author|committer|tagger) (.*) <([^<>]*)> \d+ [+-]\d{4}")


class AcceptError(RuntimeError):
    pass


# What acceptance reads: branches and tags, never refs/pull/* or other refs.
FETCH_REFSPECS = ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")


def clone(remote: str, dest: Path) -> None:
    """Default seam: a bare repo fetching only FETCH_REFSPECS (read-only, no
    prompt, bounded)."""
    if remote.startswith("-"):
        raise AcceptError(f"remote {remote!r} looks like an option")
    try:
        subprocess.run(["git", "init", "-q", "--bare", "--template=", str(dest)], check=True,
                       capture_output=True, text=True, env=git_ops._local_env())
        subprocess.run(["git", *git_ops._SAFE_CONFIG, "fetch", "-q", "--no-tags",
                        "--no-write-fetch-head", remote, *FETCH_REFSPECS],
                       cwd=dest, check=True, capture_output=True, text=True,
                       env=git_ops._network_env(), timeout=git_ops._NETWORK_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise AcceptError(f"cloning {remote} timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise AcceptError(f"cloning {remote} failed: {git_ops._stderr(exc)}") from exc


def _git(repo: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *git_ops._SAFE_CONFIG, "-c", "core.quotePath=false", *args],
                          cwd=repo, check=True, capture_output=True, text=text,
                          env=git_ops._local_env())


class _Blobs:
    """`git cat-file --batch`, one process for every blob."""

    def __init__(self, repo: Path):
        self.proc = subprocess.Popen(["git", *git_ops._SAFE_CONFIG, "cat-file", "--batch"],
                                     cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     env=git_ops._local_env())

    def read(self, oid: str) -> bytes:
        self.proc.stdin.write(f"{oid}\n".encode())
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) != 3:
            raise AcceptError(f"cannot read object {oid}")
        data = self.proc.stdout.read(int(header[2]))
        self.proc.stdout.read(1)
        return data

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait()


def _tree(repo: Path, rev: str) -> list[tuple[str, str, str, str]]:
    out = _git(repo, "ls-tree", "-r", "-z", rev).stdout
    rows = []
    for rec in filter(None, out.split("\0")):
        meta, path = rec.split("\t", 1)
        mode, kind, oid = meta.split()
        rows.append((mode, kind, oid, path))
    return rows


def _identity_header(text: str, line_no: int, identity: tuple[str, str]) -> bool:
    """Is line `line_no` (deny.scan_text numbering) of object `text` a header
    identity line naming exactly `identity`? Headers end at the first empty
    line; anything after it is the message body and never qualifies."""
    for i, line in enumerate(text.splitlines(), 1):
        if line == "":
            return False
        if i == line_no:
            m = _IDENT_HEADER.fullmatch(line)
            return bool(m) and (m.group(2), m.group(3)) == identity
    return False


def scan(root: Path, repo: Path, patterns: list,
         identity: tuple[str, str] | None = None,
         allows: tuple[deny.Allow, ...] = ()) -> tuple[structural.Report, dict]:
    """Scan the bare clone `repo`; `root` (the private checkout) resolves the
    upstream's lockfile. Returns the report and counts for the summary.

    `identity` (name, email), when given, excuses deny hits on commit/tag
    header identity lines that equal it exactly; the count is `excused`.
    `allows` ([[allow]] literals) apply to file content at HEAD and in history,
    scoped by path — a history blob is judged at EVERY path it appeared at, so
    it is excused only where each of those paths is in scope; never to path
    names, commits or tags."""
    rep = structural.Report()
    excused = 0
    r = _git(repo, "rev-parse", "-q", "--verify", "refs/heads/main^{commit}", text=True)
    tip = r.stdout.strip()
    message = _git(repo, "cat-file", "commit", tip).stdout.partition("\n\n")[2]

    approved = None
    prefix = gate.parse_upstream(message)
    if prefix:
        up = subprocess.run(["git", "rev-parse", "-q", "--verify", f"{prefix}^{{commit}}"],
                            cwd=root, capture_output=True, text=True)
        if up.returncode == 0:
            approved = lock.load_at_ref(root, up.stdout.strip())
    blobs = _Blobs(repo)
    try:
        head_rows = _tree(repo, tip)
        allow_binary: tuple[str, ...] = ()
        for _mode, _kind, oid, path in head_rows:
            if path == "publish.manifest.toml":
                with tempfile.TemporaryDirectory(prefix="kb-accept-m-") as td:
                    mp = Path(td) / "m.toml"
                    mp.write_bytes(blobs.read(oid))
                    allow_binary = manifest.load_manifest(mp).allow_binary
        head_pairs = set()
        for mode, kind, oid, path in head_rows:
            head_pairs.add((oid, path))
            if kind != "blob" or mode == "120000":
                rep.hits.append(deny.Hit(path, 0, f"structural: {kind} mode {mode} at HEAD",
                                         "(kb-publish never publishes symlinks or submodules)"))
                continue
            structural.scan_blob(rep, path, blobs.read(oid), patterns,
                                 is_pinned=lock.pinned_hook(approved),
                                 allow_binary=allow_binary, allows=allows)

        # Every (blob, path) pair, not every blob: the allow is scoped by path, so
        # one blob that sat both in and out of scope (or that HEAD holds in scope
        # and history held elsewhere) must be judged at each path it had.
        commits = _git(repo, "rev-list", "--all").stdout.split()
        history = {}
        for c in commits:
            for mode, kind, oid, path in _tree(repo, c):
                if kind == "blob" and (oid, path) not in head_pairs:
                    history.setdefault((oid, path), c)
        for (oid, path), c in sorted(history.items(), key=lambda kv: (kv[0][1], kv[1])):
            part = structural.Report()
            structural.scan_blob(part, path, blobs.read(oid), patterns, deny_only=True,
                                 allows=allows)
            rep.hits.extend(deny.Hit(f"{h.path} (history: blob {oid[:12]} in commit "
                                     f"{c[:12]})", h.line_no, h.pattern, h.line)
                            for h in part.hits)
    finally:
        blobs.close()

    objects = [(f"commit object {c[:12]}", _git(repo, "cat-file", "commit", c).stdout)
               for c in commits]
    tags = [t for t in _git(repo, "for-each-ref", "--format=%(objectname) %(objecttype) "
                            "%(refname)", "refs/tags").stdout.splitlines() if t]
    for line in tags:
        oid, kind, ref = line.split(" ", 2)
        rep.hits.extend(deny.scan_text(ref, ref, patterns))
        if kind == "tag":
            objects.append((f"tag object {ref}", _git(repo, "cat-file", "tag", oid).stdout))
    for label, text in objects:
        for h in deny.scan_text(label, text, patterns):
            if identity is not None and _identity_header(text, h.line_no, identity):
                excused += 1
            else:
                rep.hits.append(h)
        rep.hits.extend(structural.scan_content(label, text))
    return rep, {"files": len(head_rows), "history": len(history), "commits": len(commits),
                 "tags": len(tags), "excused": excused}
