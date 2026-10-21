"""Fold a stream of ChangeRecords into a stable `current path -> file_id` map.

The canonical identity of a file is its **birth** — the (commit, path) where it first
appeared — hashed to a UUID. That id is then *carried forward* across renames, so the
current path is just where the file lives now, never what it is. Because birth is fixed in
history, replaying the same log always reconstructs the same ids: identity is deterministic
and needs no persisted UUID table (a database only adds incremental replay + version audit).

Pure data structure — no git, no DB — so the matching rules are exhaustively unit-testable.
"""

from dataclasses import dataclass, field
from uuid import UUID, uuid5

from kb_core.adapters.git.history import ChangeRecord

# Dedicated namespace so a file id is a pure function of its birth and nothing else.
GIT_FILE_NAMESPACE = UUID("f1e2d3c4-b5a6-4978-8765-4321fedcba98")


def mint_file_id(birth_commit: str, birth_path: str) -> str:
    """Deterministic id for a file, from where/when it was first seen."""
    return str(uuid5(GIT_FILE_NAMESPACE, f"{birth_commit}:{birth_path}"))


@dataclass
class IdentityTracker:
    """Replays ChangeRecords (oldest-first) maintaining the live path->id assignment."""
    _by_path: dict[str, str] = field(default_factory=dict)   # current path  -> file_id
    _tombstoned: dict[str, str] = field(default_factory=dict)  # deleted path -> file_id

    def apply(self, ch: ChangeRecord) -> None:
        if ch.change_type in ("A", "M", "T"):
            # An add is a birth; a modify/typechange to an already-tracked path leaves
            # identity untouched. A modify with no known path (history started mid-life,
            # e.g. a pathspec that began matching) is treated as a birth so nothing is lost.
            if ch.change_type == "A" or ch.path not in self._by_path:
                self._birth(ch.path, ch.commit)
        elif ch.change_type == "D":
            fid = self._by_path.pop(ch.path, None)
            if fid is not None:
                self._tombstoned[ch.path] = fid     # eligible to resurrect on re-add
        elif ch.change_type == "R":
            fid = self._by_path.pop(ch.old_path, None) if ch.old_path else None
            if fid is None:
                self._birth(ch.path, ch.commit)     # renamed from an untracked path
            else:
                self._by_path[ch.path] = fid        # carry identity across the move

    def _birth(self, path: str, commit: str) -> None:
        # Re-adding a file at a tombstoned path resurrects its prior identity rather than
        # minting a stranger; otherwise mint from this birth event.
        fid = self._tombstoned.pop(path, None) or mint_file_id(commit, path)
        self._by_path[path] = fid

    @property
    def current(self) -> dict[str, str]:
        """The live `path -> file_id` map (a copy; callers may keep it)."""
        return dict(self._by_path)
