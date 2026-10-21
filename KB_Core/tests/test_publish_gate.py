"""The publish gates that compare the sha against what is already public.

- no rollback: the last published upstream sha (from public HEAD's
  "sync: <date> (upstream <sha7>)", cross-checked with private state) must be
  an ancestor of the sha being published;
- gate-file diff: changes to the files that define the gate itself since that
  sha, and deny patterns that disappeared, are listed for acknowledgement;
- origin ancestry: the sha must be on origin/main after a fetch.
"""
import subprocess

import pytest

from kb_core.publish import gate


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _commit(root, msg="c", **files):
    for rel, text in files.items():
        _write(root, rel.replace("__", "/"), text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    return root


# --- gate files ------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "publish.deny.toml", "publish.manifest.toml", ".gitleaks.toml", ".gitleaksignore",
    ".gitattributes", "KB_Core/.gitattributes", ".github/workflows/ci.yml",
    ".github/workflows/publish-candidate.yml", ".GitHub/x.yml",
    "KB_Core/src/kb_core/publish/cli.py", "KB_Core/src/kb_core/publish/new_gate.py",
    # what ci.yml / verify / export-ci actually run: editing one is editing CI
    "scripts/ci/python.sh", "scripts/ci/prepare_test_dbs.py", "Scripts/CI/new.sh",
    # An approval of export paths is acknowledged like any other gate change
    "private/publish.paths.lock",
])
def test_gate_files(rel):
    assert gate.is_gate_file(rel)


@pytest.mark.parametrize("rel", [
    "README.md", "KB_Core/src/kb_core/cli.py", "KB_Core/tests/test_publish_cli.py",
    "docs/publish.deny.toml.md", "x/.github/README.md", "KB_Core/src/kb_core/publisher.py",
    "KB_Core/scripts/ci/x.sh", "scripts/cix.sh", "private/publish.paths.lock.tmp",
    "private/notes.md",
])
def test_not_gate_files(rel):
    assert not gate.is_gate_file(rel)


def test_gate_changes_lists_only_gate_files(repo):
    base = _commit(repo, **{
        "publish.deny.toml": '[deny]\npatterns = ["alpha", "beta"]\n',
        "src/a.py": "a = 1\n", ".github/workflows/ci.yml": "on: push\n"})
    target = _commit(repo, **{"src/a.py": "a = 2\n", ".gitattributes": "* text\n",
                              ".github/workflows/ci.yml": "on: [push]\n"})
    assert gate.gate_changes(repo, base, target) == [
        ("A", ".gitattributes"), ("M", ".github/workflows/ci.yml")]
    assert gate.gate_changes(repo, target, target) == []


def test_gate_changes_first_publish_lists_every_gate_file(repo):
    target = _commit(repo, **{"publish.deny.toml": '[deny]\npatterns = ["a"]\n',
                              "src/a.py": "x\n"})
    assert gate.gate_changes(repo, None, target) == [("A", "publish.deny.toml")]


def test_gate_changes_reports_deletion(repo):
    base = _commit(repo, **{".gitleaks.toml": "x\n", "src/a.py": "x\n"})
    _git(repo, "rm", "-q", ".gitleaks.toml")
    _git(repo, "commit", "-qm", "rm")
    assert gate.gate_changes(repo, base, _git(repo, "rev-parse", "HEAD")) == [
        ("D", ".gitleaks.toml")]


def test_removed_deny_patterns(repo):
    base = _commit(repo, **{"publish.deny.toml": '[deny]\npatterns = ["alpha", "beta"]\n'})
    same = _commit(repo, **{"publish.deny.toml": '[deny]\npatterns = ["beta", "alpha", "gamma"]\n'})
    weaker = _commit(repo, **{"publish.deny.toml": '[deny]\npatterns = ["alpha"]\n'})
    assert gate.removed_deny_patterns(repo, base, same) == []
    assert gate.removed_deny_patterns(repo, base, weaker) == ["beta"]
    assert gate.removed_deny_patterns(repo, None, weaker) == []
    _git(repo, "rm", "-q", "publish.deny.toml")
    _git(repo, "commit", "-qm", "rm")
    assert gate.removed_deny_patterns(repo, base, _git(repo, "rev-parse", "HEAD")) == [
        "alpha", "beta"]


# --- upstream sha ----------------------------------------------------------------

@pytest.mark.parametrize("msg,want", [
    ("sync: 2026-10-14 (upstream abc1234)", "abc1234"),
    ("sync: 2026-10-14 (upstream abc1234)\n\nCo-authored-by: P <p@example.com>\n", "abc1234"),
    ("sync: 2026-10-14 (upstream " + "a" * 40 + ")", "a" * 40),
    ("Clarify README", None),
    ("sync: 2026-10-14", None),
    ("sync: 2026-10-14 (upstream ABC1234)", None),
    ("sync: 2026-10-14 (upstream abc12)", None),
    ("x sync: 2026-10-14 (upstream abc1234)", None),
])
def test_parse_upstream(msg, want):
    assert gate.parse_upstream(msg) == want


# --- base: public HEAD cross-checked with state --------------------------------

def _head(sha7, paths=("src/a.py",)):
    return gate.PublicHead(sha="f" * 40,
                           message=f"sync: 2026-10-14 (upstream {sha7})\n",
                           paths=list(paths))


def test_base_first_publish(repo):
    _commit(repo, **{"a": "1"})
    b = gate.resolve_base(repo, head=None, public_read=True, state_ref=None)
    assert b.sha is None
    b = gate.resolve_base(repo, head=None, public_read=False, state_ref=None)
    assert b.sha is None


def test_base_from_public_head(repo):
    one = _commit(repo, **{"a": "1"})
    b = gate.resolve_base(repo, head=_head(one[:7]), public_read=True, state_ref=one)
    assert b.sha == one and "public" in b.source


def test_base_public_ahead_of_state_is_a_note(repo):
    one = _commit(repo, **{"a": "1"})
    two = _commit(repo, **{"a": "2"})
    b = gate.resolve_base(repo, head=_head(two[:7]), public_read=True, state_ref=one)
    assert b.sha == two and b.notes


def test_base_state_without_public_read(repo):
    one = _commit(repo, **{"a": "1"})
    b = gate.resolve_base(repo, head=None, public_read=False, state_ref=one)
    assert b.sha == one and "state" in b.source


@pytest.mark.parametrize("case", ["empty-but-recorded", "foreign-head", "unknown-sha",
                                  "public-behind-state", "diverged"])
def test_base_inconsistencies_refuse(repo, case):
    one = _commit(repo, **{"a": "1"})
    two = _commit(repo, **{"a": "2"})
    _git(repo, "checkout", "-q", "-b", "side", one)
    side = _commit(repo, **{"a": "side"})
    args = {
        "empty-but-recorded": dict(head=None, state_ref=one),
        "foreign-head": dict(head=gate.PublicHead("f" * 40, "Clarify README\n", []),
                             state_ref=None),
        "unknown-sha": dict(head=_head("0123456"), state_ref=None),
        "public-behind-state": dict(head=_head(one[:7]), state_ref=two),
        "diverged": dict(head=_head(side[:7]), state_ref=two),
    }[case]
    with pytest.raises(gate.GateError):
        gate.resolve_base(repo, public_read=True, **args)


def test_no_rollback(repo):
    one = _commit(repo, **{"a": "1"})
    two = _commit(repo, **{"a": "2"})
    gate.check_no_rollback(repo, None, one)
    gate.check_no_rollback(repo, one, two)
    gate.check_no_rollback(repo, two, two)
    with pytest.raises(gate.GateError, match="rollback"):
        gate.check_no_rollback(repo, two, one)
    _git(repo, "checkout", "-q", "-b", "side", one)
    side = _commit(repo, **{"a": "side"})
    with pytest.raises(gate.GateError):
        gate.check_no_rollback(repo, two, side)


# --- reading public HEAD (a local bare repo stands in for the remote) -----------

def test_read_public_head(repo, tmp_path):
    remote = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(remote)],
                   check=True)
    assert gate.read_public_head(str(remote)) is None          # empty: no main
    _commit(repo, "sync: 2026-10-14 (upstream abc1234)", **{"src/a.py": "1", "b.md": "x"})
    _git(repo, "push", "-q", str(remote), "HEAD:refs/heads/main")
    head = gate.read_public_head(str(remote))
    assert head.sha == _git(repo, "rev-parse", "HEAD")
    assert gate.parse_upstream(head.message) == "abc1234"
    assert head.paths == ["b.md", "src/a.py"]


def test_read_public_head_unreachable_raises(tmp_path):
    with pytest.raises(gate.GateError):
        gate.read_public_head(str(tmp_path / "nope.git"))


def test_read_public_head_branches_but_no_main(repo, tmp_path):
    remote = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _commit(repo, **{"a": "1"})
    _git(repo, "push", "-q", str(remote), "HEAD:refs/heads/other")
    with pytest.raises(gate.GateError, match="no main"):
        gate.read_public_head(str(remote))


# --- origin ancestry --------------------------------------------------------------

def test_fetch_origin_and_on_origin_main(repo, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)],
                   check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    one = _commit(repo, **{"a": "1"})
    _git(repo, "push", "-q", "origin", "main")
    two = _commit(repo, **{"a": "2"})               # not pushed
    gate.fetch_origin(repo)
    assert gate.on_origin_main(repo, one)
    assert not gate.on_origin_main(repo, two)
    other = tmp_path / "other"                      # someone else pushes `two`
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _git(repo, "push", "-q", str(other), "main:refs/heads/x")
    _git(other, "push", "-q", "origin", f"{two}:refs/heads/main")
    assert not gate.on_origin_main(repo, two)       # stale until fetched
    gate.fetch_origin(repo)
    assert gate.on_origin_main(repo, two)


def test_fetch_origin_failure_raises(repo, tmp_path):
    _git(repo, "remote", "add", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(gate.GateError):
        gate.fetch_origin(repo)
