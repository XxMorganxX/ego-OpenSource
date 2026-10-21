"""GitVersionedIdentity: an IdentityStrategy that resolves a path to its stable file id.

Built for sources pulled from a Git repo. At construction it replays the repo's history
into a `path -> file_id` map (history.py + tracker.py); thereafter `external_id` is a lookup,
so a note keeps one identity across every move, rename, and edit. A path Git has never seen
(an untracked or not-yet-committed working-tree file) falls through to a path/frontmatter
strategy, so the source degrades gracefully instead of failing.
"""

from kb_core.adapters.git.history import changes_in, iter_commits
from kb_core.adapters.git.tracker import IdentityTracker
from kb_core.adapters.identity import FrontmatterIdentity, IdentityStrategy, Observation


def build_identity_map(repo: str, ref: str = "HEAD", *, pathspec: str = "*.md",
                       find_renames: int = 40) -> dict[str, str]:
    """Replay `ref`'s history into the live `path -> file_id` map at that ref."""
    tracker = IdentityTracker()
    for commit in iter_commits(repo, ref):
        for change in changes_in(repo, commit, pathspec=pathspec, find_renames=find_renames):
            tracker.apply(change)
    return tracker.current


class GitVersionedIdentity:
    """Resolve `obs.rel_path` to its carried-forward file id; fall back when untracked."""

    def __init__(self, repo: str, ref: str = "HEAD", *,
                 fallback: IdentityStrategy | None = None,
                 pathspec: str = "*.md", find_renames: int = 40) -> None:
        self._fallback = fallback or FrontmatterIdentity()
        self._map = build_identity_map(
            repo, ref, pathspec=pathspec, find_renames=find_renames,
        )

    def external_id(self, obs: Observation) -> str:
        fid = self._map.get(obs.rel_path)
        return fid if fid is not None else self._fallback.external_id(obs)
