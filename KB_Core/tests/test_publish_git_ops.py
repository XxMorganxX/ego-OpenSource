import re
import subprocess

from kb_core.publish import git_ops, state
from kb_core.publish.target import Target

# A push needs a target cli.run validated and deny patterns to scan the
# commit/tag objects with; these tests exercise the tree mechanics.
PUSH = {"target": Target(full_name="acme/mirror", repo_id=2002, empty=True),
        "deny_patterns": [re.compile("secretname", re.IGNORECASE)]}
DRY = {"deny_patterns": PUSH["deny_patterns"]}


def _bare(tmp_path):
    remote = tmp_path / "public.git"
    # Explicit main: the real remote (GitHub) defaults to main, but `git init` defaults to
    # master wherever init.defaultBranch is unset (CI's Ubuntu git; Apple's git sets it).
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(remote)],
                   check=True)
    return remote


def _tree(tmp_path):
    t = tmp_path / "tree"
    (t / "src").mkdir(parents=True)
    (t / "src" / "a.py").write_text("a = 1\n")
    (t / "README.md").write_text("hello\n")
    return t


def test_first_publish_to_empty_remote(tmp_path):
    remote, tree = _bare(tmp_path), _tree(tmp_path)
    sha = git_ops.publish_tree(
        tree, str(remote), message="sync: 2026-08-31 (upstream abc1234)",
        coauthors=[{"name": "Pat", "email": "pat@example.com"}],
        push=True, tag="v0.1.0", workdir=tmp_path / "w1", **PUSH)
    log = subprocess.run(["git", "log", "--format=%H%n%B", "main"],
                         cwd=remote, capture_output=True, text=True).stdout
    assert sha in log
    assert "Co-authored-by: Pat <pat@example.com>" in log
    tags = subprocess.run(["git", "tag"], cwd=remote, capture_output=True, text=True).stdout
    assert "v0.1.0" in tags


def test_second_publish_replaces_tree(tmp_path):
    remote, tree = _bare(tmp_path), _tree(tmp_path)
    git_ops.publish_tree(tree, str(remote), message="sync: one",
                         coauthors=[], push=True, tag=None, workdir=tmp_path / "w1", **PUSH)
    (tree / "src" / "a.py").unlink()
    (tree / "src" / "b.py").write_text("b = 2\n")
    git_ops.publish_tree(tree, str(remote), message="sync: two",
                         coauthors=[], push=True, tag=None, workdir=tmp_path / "w2", **PUSH)
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"],
                           cwd=remote, capture_output=True, text=True).stdout.split()
    assert "src/b.py" in files and "src/a.py" not in files


def test_no_push_stays_local(tmp_path):
    remote, tree = _bare(tmp_path), _tree(tmp_path)
    git_ops.publish_tree(tree, str(remote), message="sync: dry",
                         coauthors=[], push=False, tag=None, workdir=tmp_path / "w1", **DRY)
    refs = subprocess.run(["git", "show-ref"], cwd=remote,
                          capture_output=True, text=True).stdout
    assert refs.strip() == ""


def test_state_roundtrip(tmp_path):
    (tmp_path / "private").mkdir()
    assert state.load(tmp_path)["last_published_ref"] is None
    state.save(tmp_path, {"last_published_ref": "abc", "pending_coauthors": []})
    assert state.load(tmp_path)["last_published_ref"] == "abc"


def test_default_load_returns_fresh_lists(tmp_path):
    a = state.load(tmp_path)
    a["pending_coauthors"].append({"name": "X", "email": "x@example.com"})
    b = state.load(tmp_path)
    assert b["pending_coauthors"] == []


def test_coauthor_trailer_injection_is_sanitized(tmp_path):
    remote, tree = _bare(tmp_path), _tree(tmp_path)
    evil_name = "Evil\n\nSigned-off-by: Fake Admin <a@b.c>"
    git_ops.publish_tree(
        tree, str(remote), message="sync: injection-check",
        coauthors=[{"name": evil_name, "email": "pat@example.com"}],
        push=True, tag=None, workdir=tmp_path / "w1", **PUSH)
    body = subprocess.run(["git", "log", "-1", "--format=%B", "main"],
                          cwd=remote, capture_output=True, text=True).stdout
    lines = body.splitlines()
    coauthor_lines = [line for line in lines if line.startswith("Co-authored-by:")]
    assert len(coauthor_lines) == 1
    assert not any(line.startswith("Signed-off-by:") for line in lines)
    assert coauthor_lines[0] == (
        "Co-authored-by: Evil Signed-off-by: Fake Admin <a@b.c> <pat@example.com>")


def test_clone_failure_warns_before_fallback(tmp_path, capsys):
    tree = _tree(tmp_path)
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    (not_a_repo / "somefile.txt").write_text("not a repo\n")
    git_ops.publish_tree(tree, str(not_a_repo), message="sync: warn",
                         coauthors=[], push=False, tag=None, workdir=tmp_path / "w1", **DRY)
    captured = capsys.readouterr()
    assert "[git_ops] clone of" in captured.out
    assert "assuming empty remote" in captured.out


# --- a dry run with no remote never touches the network -----------------------

def test_dry_run_without_remote_never_clones(tmp_path, monkeypatch):
    def _no_clone(*a, **kw):
        raise AssertionError("a remote-less dry run must not clone")

    monkeypatch.setattr(git_ops, "_clone", _no_clone)
    sha = git_ops.publish_tree(_tree(tmp_path), None, message="sync: dry", coauthors=[],
                               push=False, tag="v0.1.0", workdir=tmp_path / "w", **DRY)
    assert re.fullmatch(r"[0-9a-f]{40}", sha)


def test_push_without_remote_refused(tmp_path):
    import pytest
    with pytest.raises(git_ops.PublishRefused):
        git_ops.publish_tree(_tree(tmp_path), None, message="sync: x", coauthors=[],
                             push=True, tag=None, workdir=tmp_path / "w", **PUSH)
