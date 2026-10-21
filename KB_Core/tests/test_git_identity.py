"""Integration: identity over a real throwaway repo. Proves a file keeps one id across
move + rename + edit — the design's worked example, executable."""

import subprocess

import pytest

from kb_core.adapters.git.identity import GitVersionedIdentity, build_identity_map
from kb_core.adapters.identity import Observation


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def _commit(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_identity_survives_move_rename_and_edit(repo):
    (repo / "Projects").mkdir()
    (repo / "Projects" / "AI.md").write_text("# AI\noriginal body, reasonably long\n")
    c1 = _commit(repo, "add AI.md")
    at_c1 = build_identity_map(str(repo), c1)
    assert list(at_c1) == ["Projects/AI.md"]
    fid = at_c1["Projects/AI.md"]

    # Move to a new folder, rename, AND edit the body — all at once.
    (repo / "Archive").mkdir()
    (repo / "Projects" / "AI.md").unlink()
    (repo / "Archive" / "AI Notes.md").write_text(
        "# AI\noriginal body, reasonably long\nplus a new appended line\n")
    _commit(repo, "move+rename+edit")

    at_head = build_identity_map(str(repo), "HEAD")
    assert list(at_head) == ["Archive/AI Notes.md"]
    assert at_head["Archive/AI Notes.md"] == fid          # same identity, new path


def test_strategy_resolves_tracked_and_falls_back_for_untracked(repo):
    (repo / "kept.md").write_text("a stable note with enough text to exist\n")
    _commit(repo, "add kept")
    ident = GitVersionedIdentity(str(repo))

    tracked = ident.external_id(Observation(rel_path="kept.md"))
    assert tracked == build_identity_map(str(repo), "HEAD")["kept.md"]

    # An untracked working-tree path Git has never seen falls through to frontmatter/path.
    assert ident.external_id(Observation(rel_path="draft.md")) == "draft.md"
    assert ident.external_id(
        Observation(rel_path="draft.md", frontmatter_id="fm-1")) == "fm-1"
