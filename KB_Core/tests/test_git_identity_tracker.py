"""Pure unit tests for the identity fold — fabricated ChangeRecords, no git, no DB."""

from kb_core.adapters.git.history import ChangeRecord
from kb_core.adapters.git.tracker import IdentityTracker, mint_file_id


def _run(*changes: ChangeRecord) -> dict[str, str]:
    t = IdentityTracker()
    for c in changes:
        t.apply(c)
    return t.current


def test_add_mints_a_birth_id():
    cur = _run(ChangeRecord("c1", "A", "Projects/AI.md"))
    assert cur == {"Projects/AI.md": mint_file_id("c1", "Projects/AI.md")}


def test_mint_is_deterministic():
    assert mint_file_id("c1", "a.md") == mint_file_id("c1", "a.md")
    assert mint_file_id("c1", "a.md") != mint_file_id("c2", "a.md")


def test_rename_carries_identity():
    fid = mint_file_id("c1", "Projects/AI.md")
    cur = _run(
        ChangeRecord("c1", "A", "Projects/AI.md"),
        ChangeRecord("c2", "R", "Archive/AI Notes.md",
                     old_path="Projects/AI.md", score=87),
    )
    assert cur == {"Archive/AI Notes.md": fid}     # same id, new path only


def test_modify_does_not_change_identity():
    fid = mint_file_id("c1", "a.md")
    cur = _run(ChangeRecord("c1", "A", "a.md"), ChangeRecord("c2", "M", "a.md"))
    assert cur == {"a.md": fid}


def test_delete_tombstones_and_drops_from_current():
    cur = _run(ChangeRecord("c1", "A", "a.md"), ChangeRecord("c2", "D", "a.md"))
    assert cur == {}


def test_readd_at_same_path_resurrects_prior_identity():
    fid = mint_file_id("c1", "a.md")
    cur = _run(
        ChangeRecord("c1", "A", "a.md"),
        ChangeRecord("c2", "D", "a.md"),
        ChangeRecord("c3", "A", "a.md"),   # a *later* birth, but resurrects c1's id
    )
    assert cur == {"a.md": fid}


def test_rename_from_untracked_path_mints_new():
    # No prior add of old_path -> the rename's target is born here.
    cur = _run(ChangeRecord("c5", "R", "new.md", old_path="ghost.md", score=90))
    assert cur == {"new.md": mint_file_id("c5", "new.md")}


def test_swap_renames_cross_identities_correctly():
    a, b = mint_file_id("c1", "a.md"), mint_file_id("c1", "b.md")
    cur = _run(
        ChangeRecord("c1", "A", "a.md"),
        ChangeRecord("c1", "A", "b.md"),
        ChangeRecord("c2", "R", "tmp.md", old_path="a.md", score=100),
        ChangeRecord("c2", "R", "a.md", old_path="b.md", score=100),
        ChangeRecord("c2", "R", "b.md", old_path="tmp.md", score=100),
    )
    assert cur == {"a.md": b, "b.md": a}     # contents swapped, identities followed
