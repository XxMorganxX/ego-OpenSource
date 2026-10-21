"""Push-gate checks that compare the sha with what is already public.

Three questions kb-publish must answer from git itself, not from trust:

- Is the sha on private origin/main? (fetch_origin + on_origin_main) A local
  branch, a worktree branch or an unpushed commit has had no CI.
- What did the last publish ship? The public HEAD names it in its message —
  "sync: <date> (upstream <sha7>)" — and private state recorded it too. They
  must agree (resolve_base); then that sha must be an ancestor of the one
  being published (check_no_rollback). Publishing an older ref used to roll
  public back silently.
- Did the gate itself change since then? A commit that weakens
  publish.deny.toml, widens the manifest or edits kb_core/publish/** passes
  the very gate it changes. gate_changes lists those files,
  removed_deny_patterns the patterns that vanished and allow_changes the
  [[allow]] exceptions that appeared or widened; a push must acknowledge
  them with --ack-gate-changes <ack_token>: a digest of the change itself,
  printed only next to the diff it acknowledges (an ack that was just the
  sha would be printed everywhere and pre-acknowledge any future change).

And is the gate RUNNING the one already public? (running_gate_problem)
Code in the commit under review — kb_core/__init__.py, kb_core/publish/** —
runs on import, before any check, and can switch the checks off. A push
therefore runs only with gate code byte-identical to that code at the last
published upstream (the sha itself on a first publish): a gate change is
judged, and its diff shown for the ack, by the previous gate.

Network reads (fetch_origin, read_public_head) are read-only and run in the
same scrubbed, non-prompting git environment git_ops uses; cli.Seams lets
tests swap them. Every failure raises GateError.
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from kb_core.publish import git_ops


class GateError(RuntimeError):
    pass


# Files that define the gate. Compared case-insensitively (fail closed).
# scripts/ci/ is what ci.yml, verify and export-ci actually run: a weakened
# script is a weakened CI even though the workflow file did not change.
#
# Everything that EXECUTES inside the gate process is gate too: kb-publish
# and the strict guard import kb_core, so kb_core/__init__.py runs before any
# gate code — three lines there can replace gate.gate_changes and ship a weakened
# denylist with no ack. KB_Core's pyproject/uv.lock decide what gets installed
# into that process; sitecustomize/usercustomize and *.pth run at interpreter
# start. kb_core.publish imports nothing else from kb_core (a test pins that).
# Listing them is what makes an honest gate show the change and what makes
# kb-import-pr treat it as a control file; it cannot stop code that already
# runs — that needs a non-editable install of the last published gate
# (docs/publishing.md).
# private/publish.paths.lock is the record of which paths a human approved for export:
# an approval rides in with the commit, so it is shown and acknowledged like any
# other gate change instead of passing silently.
_GATE_EXACT = frozenset({
    "publish.deny.toml", "publish.manifest.toml", "private/publish.paths.lock",
    "kb_core/src/kb_core/__init__.py", "kb_core/pyproject.toml", "kb_core/uv.lock",
})
# private/githooks/: the allowlist pre-push hook kb-publish requires installed.
_GATE_PREFIXES = (".github/", "kb_core/src/kb_core/publish/", "scripts/ci/",
                  "private/githooks/")
_GATE_NAMES = frozenset({".gitattributes", "sitecustomize.py", "usercustomize.py"})


def is_gate_file(rel: str) -> bool:
    low = rel.casefold()
    name = low.rsplit("/", 1)[-1]
    return (low in _GATE_EXACT or low.startswith(_GATE_PREFIXES)
            or name.startswith(".gitleaks") or name in _GATE_NAMES or name.endswith(".pth"))


# Exactly what cli.run writes as the sync commit's first line.
_UPSTREAM = git_ops.SYNC_SUBJECT


def parse_upstream(message: str) -> str | None:
    """The private sha (prefix) a sync commit message names, else None."""
    m = _UPSTREAM.match(message.split("\n", 1)[0])
    return m[1] if m else None


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *git_ops._SAFE_CONFIG, *args], cwd=root, check=check,
                          capture_output=True, text=True, env=git_ops._local_env())


def is_ancestor(root: Path, a: str, b: str) -> bool:
    r = _git(root, "merge-base", "--is-ancestor", a, b, check=False)
    if r.returncode not in (0, 1):
        raise GateError(f"git merge-base {a[:12]} {b[:12]} failed: {r.stderr.strip()[-200:]}")
    return r.returncode == 0


# --- origin/main ------------------------------------------------------------------

ORIGIN_MAIN = "refs/remotes/origin/main"


def fetch_origin(root: Path) -> None:
    """Default seam: refresh origin/main (read-only, no prompt, bounded)."""
    try:
        subprocess.run(["git", *git_ops._SAFE_CONFIG, "fetch", "-q", "--no-tags", "origin",
                        f"+refs/heads/main:{ORIGIN_MAIN}"], cwd=root, check=True,
                       capture_output=True, text=True, env=git_ops._network_env(),
                       timeout=git_ops._NETWORK_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"git fetch origin timed out after {git_ops._NETWORK_TIMEOUT_S}s") from exc
    except subprocess.CalledProcessError as exc:
        raise GateError(f"git fetch origin failed: {git_ops._stderr(exc)}") from exc


def on_origin_main(root: Path, sha: str) -> bool:
    if _git(root, "rev-parse", "-q", "--verify", f"{ORIGIN_MAIN}^{{commit}}",
            check=False).returncode != 0:
        raise GateError(f"{ORIGIN_MAIN} does not exist after the fetch")
    return is_ancestor(root, sha, ORIGIN_MAIN)


# --- public HEAD ------------------------------------------------------------------

class PublicHead(NamedTuple):
    sha: str            # the public commit
    message: str        # its full message
    paths: list[str]    # every file in its tree, sorted


def read_public_head(remote: str) -> PublicHead | None:
    """Default seam: public main's tip (depth-1 fetch into a throwaway repo).

    None when the remote has no branches at all (a new, empty mirror). A
    remote with branches but no main is not a mirror kb-publish made.
    """
    if remote.startswith("-"):
        raise GateError(f"remote {remote!r} looks like an option")
    env = git_ops._network_env()
    try:
        ls = subprocess.run(["git", *git_ops._SAFE_CONFIG, "ls-remote", remote, "refs/heads/*"],
                            check=True, capture_output=True, text=True, env=env,
                            timeout=git_ops._NETWORK_TIMEOUT_S)
        heads = dict(reversed(line.split("\t", 1)) for line in ls.stdout.splitlines() if line)
        if not heads:
            return None
        if "refs/heads/main" not in heads:
            raise GateError(f"{remote} has branches but no main ({', '.join(sorted(heads)[:5])})")
        with tempfile.TemporaryDirectory(prefix="kb-public-head-") as td:
            repo = Path(td)
            _git(repo, "init", "-q", "--bare", "--template=")
            subprocess.run(["git", *git_ops._SAFE_CONFIG, "fetch", "-q", "--depth=1",
                            "--no-tags", remote, "+refs/heads/main:refs/heads/main"],
                           cwd=repo, check=True, capture_output=True, text=True, env=env,
                           timeout=git_ops._NETWORK_TIMEOUT_S)
            sha = _git(repo, "rev-parse", "refs/heads/main").stdout.strip()
            raw = _git(repo, "cat-file", "commit", sha).stdout
            paths = _git(repo, "ls-tree", "-r", "-z", "--name-only", sha).stdout
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"reading {remote} timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise GateError(f"reading public main from {remote} failed: "
                        f"{git_ops._stderr(exc)}") from exc
    return PublicHead(sha=sha, message=raw.partition("\n\n")[2],
                      paths=sorted(p for p in paths.split("\0") if p))


# --- the last published upstream sha ----------------------------------------------

@dataclass
class Base:
    sha: str | None                 # full private sha last published; None = first publish
    source: str
    notes: list[str] = field(default_factory=list)


def _resolve(root: Path, prefix: str) -> str:
    r = _git(root, "rev-parse", "-q", "--verify", f"{prefix}^{{commit}}", check=False)
    if r.returncode != 0:
        raise GateError(f"public HEAD names upstream {prefix}, which this repo cannot resolve "
                        f"to one commit (fetch origin? ambiguous? not this repo's mirror?)")
    return r.stdout.strip()


def resolve_base(root: Path, *, head: PublicHead | None, public_read: bool,
                 state_ref: str | None) -> Base:
    """The private sha the public mirror was last built from.

    public_read: whether `head` is what the public repo actually says (None =
    empty). Without a read only private state is known. The two must agree:
    state may lag (another checkout published since) but never lead or diverge.
    """
    if not public_read:
        if state_ref:
            return Base(state_ref, f"private state (last_published_ref {state_ref[:7]}); "
                                   f"public HEAD not read")
        return Base(None, "first publish (no recorded publish; public HEAD not read)")
    if head is None:
        if state_ref:
            raise GateError(f"public main is empty, but this checkout recorded publishing "
                            f"{state_ref[:12]} — wrong repo, or its history was deleted")
        return Base(None, "first publish (public main is empty)")
    prefix = parse_upstream(head.message)
    if prefix is None:
        subject = head.message.split("\n", 1)[0][:100]
        raise GateError(f"public HEAD {head.sha[:12]} is not a kb-publish sync commit "
                        f"({subject!r})")
    public = _resolve(root, prefix)
    base = Base(public, f"public HEAD {head.sha[:12]} (upstream {public[:7]})")
    if state_ref and state_ref != public:
        if is_ancestor(root, state_ref, public):
            base.notes.append(f"public was published from {public[:7]} after this checkout's "
                              f"last publish ({state_ref[:7]}) — another checkout?")
        else:
            raise GateError(f"public HEAD says upstream {public[:12]} but this checkout "
                            f"recorded publishing {state_ref[:12]}, which is not its "
                            f"ancestor — public was rolled back or replaced")
    return base


def check_no_rollback(root: Path, base: str | None, sha: str) -> None:
    if base and not is_ancestor(root, base, sha):
        raise GateError(f"rollback: {sha[:12]} does not contain the last published upstream "
                        f"{base[:12]} — publishing it would roll public back (or fork it)")


# --- gate-file diff ---------------------------------------------------------------

def gate_changes(root: Path, base: str | None, sha: str) -> list[tuple[str, str]]:
    """(status, path) for every gate file changed base..sha; first publish: all of them."""
    if base is None:
        out = _git(root, "-c", "core.quotePath=false", "ls-tree", "-r", "-z", "--name-only",
                   sha).stdout
        return sorted(("A", p) for p in out.split("\0") if p and is_gate_file(p))
    out = _git(root, "-c", "core.quotePath=false", "diff", "--name-status", "-z",
               "--no-renames", base, sha).stdout.split("\0")
    pairs = [(out[i], out[i + 1]) for i in range(0, len(out) - 1, 2)]
    return sorted(((s, p) for s, p in pairs if is_gate_file(p)), key=lambda x: x[1])


def _deny_strings(root: Path, sha: str) -> list[str]:
    r = _git(root, "show", f"{sha}:publish.deny.toml", check=False)
    if r.returncode != 0:
        return []
    try:
        return [p for p in tomllib.loads(r.stdout)["deny"]["patterns"] if isinstance(p, str)]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise GateError(f"publish.deny.toml at {sha[:12]} does not parse") from exc


def _allow_entries(root: Path, sha: str) -> dict[str, set[str]]:
    """{literal: its path globs} for the [[allow]] entries at `sha`."""
    r = _git(root, "show", f"{sha}:publish.deny.toml", check=False)
    if r.returncode != 0:
        return {}
    try:
        raw = tomllib.loads(r.stdout).get("allow", [])
    except tomllib.TOMLDecodeError as exc:
        raise GateError(f"publish.deny.toml at {sha[:12]} does not parse") from exc
    if not isinstance(raw, list):
        return {}
    out: dict[str, set[str]] = {}
    for e in raw:
        if isinstance(e, dict) and isinstance(e.get("text"), str):
            paths = e.get("paths")
            globs = {g for g in paths if isinstance(g, str)} if isinstance(paths, list) else set()
            out.setdefault(e["text"], set()).update(globs)
    return out


def allow_changes(root: Path, base: str | None, sha: str) -> list[str]:
    """How the [[allow]] exceptions widened from `base` to `sha` (every entry on
    a first publish): one "added literal" per literal new at `sha`, one
    "widened scope" per existing literal that gained a path glob. Removals and
    narrowing are not listed — they only shrink what may leave. Each item is a
    KIND, never the literal: a literal contains a concealed identifier."""
    before = _allow_entries(root, base) if base else {}
    out = []
    for text, globs in _allow_entries(root, sha).items():
        if text not in before:
            out.append("added literal")
        elif globs - before[text]:
            out.append("widened scope")
    return out


def removed_deny_patterns(root: Path, base: str | None, sha: str) -> list[str]:
    """Deny patterns present at `base` and gone at `sha` (a weakened denylist).

    Callers print the COUNT only: the patterns are the concealed identifiers.
    """
    if base is None:
        return []
    now = set(_deny_strings(root, sha))
    return [p for p in _deny_strings(root, base) if p not in now]


def _blobs(root: Path, rev: str, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    out = _git(root, "-c", "core.quotePath=false", "ls-tree", "-z", rev, "--",
               *paths).stdout
    blobs = {}
    for rec in filter(None, out.split("\0")):
        meta, path = rec.split("\t", 1)
        blobs[path] = meta.split()[2]
    return blobs


def ack_token(root: Path, base: str | None, sha: str, changes: list[tuple[str, str]],
              removed: list[str]) -> str | None:
    """The --ack-gate-changes value for exactly this gate change, or None if none.

    A digest of base, sha, every changed gate file's old and new blob and the
    removed-pattern count: it cannot be written down before the change exists,
    and a different change (or the same sha against another base) needs a new
    one. 16 hex digits — an acknowledgement, not a secret.
    """
    if not (changes or removed):
        return None
    paths = [p for _, p in changes]
    old = _blobs(root, base, paths) if base else {}
    new = _blobs(root, sha, paths)
    lines = ["kb-publish gate ack v1", sha, base or "-"]
    lines += [f"{s}\t{p}\t{old.get(p, '-')}\t{new.get(p, '-')}" for s, p in changes]
    lines.append(f"removed-patterns\t{len(removed)}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


# --- which gate code is running --------------------------------------------------

GATE_CODE_PREFIX = "KB_Core/src/kb_core/"


def _running_gate_code() -> dict[str, str]:
    """rel ('__init__.py', 'publish/x.py') -> git blob id of the code imported now."""
    import kb_core
    pkg = Path(kb_core.__file__).parent
    files = [pkg / "__init__.py", *sorted((pkg / "publish").glob("*.py"))]
    out = {}
    for f in files:
        data = f.read_bytes()
        out[f.relative_to(pkg).as_posix()] = hashlib.sha1(
            b"blob %d\0" % len(data) + data).hexdigest()
    return out


def running_gate_problem(root: Path, rev: str) -> str | None:
    """Why the gate code running now is not the code at `rev`, or None."""
    out = _git(root, "-c", "core.quotePath=false", "ls-tree", "-r", "-z", rev, "--",
               f"{GATE_CODE_PREFIX}__init__.py", f"{GATE_CODE_PREFIX}publish/").stdout
    committed = {}
    for rec in filter(None, out.split("\0")):
        meta, path = rec.split("\t", 1)
        rel = path[len(GATE_CODE_PREFIX):]
        if rel == "__init__.py" or (rel.startswith("publish/") and rel.endswith(".py")
                                    and rel.count("/") == 1):
            committed[rel] = meta.split()[2]
    if not committed:
        return f"{rev[:12]} has no {GATE_CODE_PREFIX} gate code to compare with"
    running = _running_gate_code()
    differ = sorted(k for k in set(running) | set(committed)
                    if running.get(k) != committed.get(k))
    if not differ:
        return None
    shown = ", ".join(f"kb_core/{k}" for k in differ[:5]) + (" ..." if len(differ) > 5 else "")
    return f"the gate code running now differs from {rev[:12]}: {shown}"
