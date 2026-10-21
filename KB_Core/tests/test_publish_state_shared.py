"""Publish state is one file per repository, not per checkout.

Publishing from a fresh `git worktree add` checkout is the normal flow. With the state
in that worktree's own private/publish-state.json the push was refused ("no
public repo is pinned"), a re-pin there let it through, and the record of
the push (last_published_*, cleared coauthors) died with the worktree — so
the ancestry check never armed, imported PR credit stayed pending in the dev
checkout, and `git worktree remove` failed on the untracked file. The state
now lives in the PRIMARY checkout (parent of the shared git dir), which every
worktree resolves to, and is gitignored so `git add -A` and commit automation
can't commit it (a branch switch then rolled the pin back).
"""
import subprocess
from pathlib import Path

from kb_core.publish import state

ROOT = Path(__file__).resolve().parents[2]


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(tmp_path):
    root = tmp_path / "dev"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "a")
    return root


def test_worktree_reads_and_writes_the_primary_checkouts_state(tmp_path):
    root = _repo(tmp_path)
    wt = tmp_path / "kb-publish-wt"
    _git(root, "worktree", "add", "-q", "--detach", str(wt), "HEAD")

    st = state.load(root)
    st.update(public_repo_id=7, public_repo_full_name="acme/mirror")
    state.save(root, st)
    assert state.load(wt)["public_repo_id"] == 7          # pin seen from the worktree

    st = state.load(wt)
    st.update(last_published_ref="a" * 40, pending_coauthors=[])
    state.save(wt, st)                                     # a push from the worktree
    assert state.load(root)["last_published_ref"] == "a" * 40
    assert not (wt / "private" / "publish-state.json").exists()
    # ... so a plain `git worktree remove` (no --force) succeeds.
    _git(root, "worktree", "remove", str(wt))


def test_state_path_is_the_primary_checkout(tmp_path):
    root = _repo(tmp_path)
    wt = tmp_path / "wt2"
    _git(root, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    assert state.path(wt) == state.path(root) == (root / "private" / "publish-state.json"
                                                  ).resolve()


def test_outside_a_git_repo_state_stays_under_root(tmp_path):
    assert state.path(tmp_path) == tmp_path / "private" / "publish-state.json"


def test_publish_state_is_gitignored():
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index",
                        "private/publish-state.json"], cwd=ROOT)
    assert r.returncode == 0
