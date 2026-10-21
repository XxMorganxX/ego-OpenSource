"""Read a repo's per-commit changes with rename detection — the input to identity.

A thin, deterministic wrapper over `git`: list the first-parent history oldest-first, then
ask `git diff-tree -M` for each commit's name-status against its parent. Git does not store
renames; it *detects* them by content similarity at diff time (`--find-renames`), and that
detection is exactly the signal we fold into stable identity (see tracker.py).

Pure of any database. The only side effect is invoking `git` on a real repository, which
makes this layer trivially testable against a throwaway repo.
"""

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class ChangeRecord:
    """One path's change in one commit, as reported by `git diff-tree --name-status`."""
    commit: str
    change_type: str            # 'A' add | 'M' modify | 'D' delete | 'R' rename | 'T' typechange
    path: str                   # the resulting path (for 'D', the path that was deleted)
    old_path: str | None = None # the prior path, for renames
    score: int | None = None    # rename similarity 0..100, for renames


def _git(repo: str, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, check=True, text=True,
    )
    return out.stdout


def iter_commits(repo: str, ref: str = "HEAD") -> list[str]:
    """First-parent history of `ref`, oldest commit first.

    `--first-parent` linearizes merges so a feature branch's churn is never replayed as
    identity events — only the canonical timeline mints and moves file ids.
    """
    return _git(repo, "rev-list", "--first-parent", "--reverse", "--root", ref).split()


def changes_in(repo: str, commit: str, *, pathspec: str = "*.md",
               find_renames: int = 40) -> list[ChangeRecord]:
    """The `pathspec` changes a commit introduced vs. its first parent, renames detected.

    `--root` makes the very first commit diff against the empty tree (all adds) instead of
    producing nothing. `-z` is NUL-delimited so paths with spaces parse unambiguously.
    """
    raw = _git(
        repo, "diff-tree", "-r", "-z", "--no-commit-id", "--name-status", "--root",
        f"--find-renames={find_renames}%", commit, "--", pathspec,
    )
    return list(_parse_name_status(commit, raw))


def _parse_name_status(commit: str, raw: str) -> Iterator[ChangeRecord]:
    # `-z --name-status` is a flat NUL-separated stream: a status token, then one path
    # (A/M/D/T) or two paths (R/C: old then new). We pull tokens in that shape.
    tokens = [t for t in raw.split("\0") if t != ""]
    i = 0
    while i < len(tokens):
        status = tokens[i]
        code = status[0]
        if code in ("R", "C"):
            old, new = tokens[i + 1], tokens[i + 2]
            score = int(status[1:]) if status[1:].isdigit() else None
            # A copy ('C') is a new logical file (a duplicate), so we surface it as an add
            # of `new`; only a true rename carries identity. Renames keep both endpoints.
            if code == "R":
                yield ChangeRecord(commit, "R", new, old_path=old, score=score)
            else:
                yield ChangeRecord(commit, "A", new)
            i += 3
        else:
            yield ChangeRecord(commit, code, tokens[i + 1])
            i += 2
