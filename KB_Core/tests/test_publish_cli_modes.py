"""`kb-publish export` and `kb-publish diff` — the CI-facing modes.

export: the exact export a push would build, with every content gate, into a
directory — no clone, no push, no network. diff: added/removed paths and gate
changes against what is public, as Markdown for a job summary.
"""
import subprocess

import pytest

from kb_core.publish import cli, lock, state
from tests.publish_fakes import PUBLIC_URL, ack, gated, head, push_origin, wire_public

MANIFEST = """
[publish]
remote = "https://github.com/example/ego-OS.git"
[export]
include = ["src/**", "publish.manifest.toml"]
"""
DENY = '[deny]\npatterns = ["secretname", "othername"]\n'


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(root, msg="c", approve=True, **files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)
    if approve and cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0 and _git(
            root, "status", "--porcelain", lock.LOCK_REL):
        _git(root, "add", lock.LOCK_REL)
        _git(root, "commit", "-qm", "approve")
    return head(root)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _commit(root, "init", **{"publish.manifest.toml": MANIFEST, "publish.deny.toml": DENY,
                             "src/a.py": "a = 1\n", "src/b.py": "b = 1\n"})
    return root


@pytest.fixture
def no_network(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("must not touch a remote")
    monkeypatch.setattr(cli.git_ops, "_clone", boom)
    monkeypatch.setattr(cli.gate, "read_public_head", boom)
    monkeypatch.setattr(cli.gate, "fetch_origin", boom)
    monkeypatch.delenv("KB_PUBLISH_REMOTE", raising=False)


# --- export --------------------------------------------------------------------------

def test_export_writes_the_gated_tree(repo, tmp_path, capsys, no_network):
    out = tmp_path / "out"
    assert cli.export_tree(repo, ref="HEAD", out=out) == 0
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert files == ["publish.manifest.toml", "src/a.py", "src/b.py"]
    assert "ok" in capsys.readouterr().out


@pytest.mark.parametrize("files,approve,needle", [
    ({"src/c.py": "# secretname\n"}, True, "denylist/structural"),
    ({"src/new.py": "n = 1\n"}, False, "not approved"),
    ({"src/shot.png": "x"}, True, "media"),
])
def test_export_refuses_and_leaves_nothing(repo, tmp_path, capsys, no_network,
                                           files, approve, needle):
    _commit(repo, "x", approve=approve, **files)
    out = tmp_path / "out"
    assert cli.export_tree(repo, ref="HEAD", out=out) == 1
    o = capsys.readouterr().out
    assert "REFUSED" in o and needle in o
    assert not out.exists()          # a refused tree can't be uploaded as if it passed


def test_export_refuses_non_empty_out_without_touching_it(repo, tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine")
    assert cli.export_tree(repo, ref="HEAD", out=out) == 1
    assert (out / "keep.txt").read_text() == "mine"


def test_export_refuses_without_deny_file(repo, tmp_path, no_network):
    _git(repo, "rm", "-q", "publish.deny.toml")
    _git(repo, "commit", "-qm", "rm")
    assert cli.export_tree(repo, ref="HEAD", out=tmp_path / "out") == 1


def test_main_export_subcommand(repo, tmp_path, monkeypatch, no_network):
    monkeypatch.chdir(repo)
    assert cli.main(["export", "--ref", "HEAD", "--out", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "src" / "a.py").exists()


# --- diff ----------------------------------------------------------------------------

def test_diff_first_publish_offline(repo, capsys, no_network):
    sha = head(repo)
    assert cli.diff(repo, ref="HEAD") == 0
    out = capsys.readouterr().out
    assert "3 added, 0 removed" in out and "+ src/a.py" in out
    assert "first publish" in out
    assert "publish.deny.toml" in out and f"--ack-gate-changes {ack(repo)}" in out
    assert f"--ack-gate-changes {sha}" not in out


def test_diff_against_state_without_network(repo, capsys, no_network):
    one = head(repo)
    st = state.load(repo)
    st["last_published_ref"] = one
    state.save(repo, st)
    _git(repo, "rm", "-q", "src/b.py")
    _commit(repo, "edit", **{"src/c.py": "c = 1\n"})
    assert cli.diff(repo, ref="HEAD") == 0
    out = capsys.readouterr().out
    assert "1 added, 1 removed" in out and "+ src/c.py" in out and "- src/b.py" in out
    # The fixture re-approves c.py, so the lockfile changed: that IS a gate change.
    assert "1 gate file(s) changed" in out and "private/publish.paths.lock" in out
    assert "--ack-gate-changes" in out


def test_diff_reads_public_head_and_reports_gate_changes(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_NAME", "ego-OS mirror")
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_EMAIL", "mirror@invalid")
    remote, gh = wire_public(tmp_path, repo, monkeypatch)
    push_origin(repo)
    one = head(repo)
    assert cli.run(repo, ref="HEAD", push=True, tag=None, remote=PUBLIC_URL, github=gh,
                   **gated(repo)) == 0
    two = _commit(repo, "weaken", **{"publish.deny.toml": '[deny]\npatterns = ["secretname"]\n',
                                     "src/d.py": "d = 1\n"})
    capsys.readouterr()
    assert cli.diff(repo, ref="HEAD", remote=PUBLIC_URL) == 0
    out = capsys.readouterr().out
    assert f"upstream {one[:7]}" in out and "public HEAD" in out
    assert "+ src/d.py" in out and "1 added, 0 removed" in out
    assert "M publish.deny.toml" in out
    assert "1 deny pattern(s) removed" in out and "othername" not in out
    assert f"--ack-gate-changes {ack(repo)}" in out and two not in out.split("--ack", 1)[1]


def test_diff_unreadable_public_falls_back_with_note(repo, capsys):
    def unreadable(remote):
        raise cli.gate.GateError("HTTP 404")
    s = cli.Seams(public_head=unreadable)
    assert cli.diff(repo, ref="HEAD", remote="https://github.com/acme/mirror.git", seams=s) == 0
    out = capsys.readouterr().out
    assert "NOTE" in out and "HTTP 404" in out and "first publish" in out


def test_diff_pinned_repo_is_read_anonymously(repo, capsys, monkeypatch):
    monkeypatch.delenv("KB_PUBLISH_REMOTE", raising=False)
    st = state.load(repo)
    st.update(public_repo_id=2002, public_repo_full_name="acme/mirror")
    state.save(repo, st)
    seen = []
    s = cli.Seams(public_head=lambda remote: seen.append(remote))
    assert cli.diff(repo, ref="HEAD", seams=s) == 0
    assert seen == ["https://github.com/acme/mirror.git"]


def test_diff_rollback_refused(repo, capsys, no_network):
    one = head(repo)
    two = _commit(repo, "two", **{"src/a.py": "a = 2\n"})
    st = state.load(repo)
    st["last_published_ref"] = two
    state.save(repo, st)
    assert cli.diff(repo, ref=one) == 1
    assert "rollback" in capsys.readouterr().out


def test_main_diff_subcommand(repo, monkeypatch, capsys, no_network):
    monkeypatch.chdir(repo)
    assert cli.main(["diff"]) == 0
    assert "kb-publish diff" in capsys.readouterr().out


def test_diff_stdout_is_only_the_summary(repo, capsys, no_network):
    # stdout goes to $GITHUB_STEP_SUMMARY: assembly chatter belongs on stderr.
    (repo / "src" / "link").symlink_to("a.py")
    _commit(repo, "link")
    capsys.readouterr()
    assert cli.diff(repo, ref="HEAD") == 0
    cap = capsys.readouterr()
    assert cap.out.startswith("### kb-publish diff")
    assert "stripped symlink" in cap.err and "stripped symlink" not in cap.out
    assert "  - `A publish.deny.toml`" in cap.out


# --- diff: content of already-exported files (the lockfile gates new paths only) ----

def _published_then_edited(repo):
    _commit(repo, "fixture", **{"src/fixtures/notes.txt": "synthetic line\n",
                                "notes/private.txt": "private\n"})
    st = state.load(repo)
    st["last_published_ref"] = head(repo)
    state.save(repo, st)
    _commit(repo, "edit", **{"src/a.py": "a = 2\n",
                             "src/fixtures/notes.txt": "synthetic line\npasted corpus line\n",
                             "notes/private.txt": "still private, changed\n",
                             "src/new.py": "n = 1\n"})


def test_diff_prints_a_content_stat_for_exported_edits(repo, capsys, no_network):
    _published_then_edited(repo)
    capsys.readouterr()
    assert cli.diff(repo, ref="HEAD") == 0
    out = capsys.readouterr().out
    assert "**2 already-exported file(s) changed, +2 -1**" in out
    assert "src/a.py               | +1 -1" in out
    assert "src/fixtures/notes.txt | +1 -0" in out
    assert "notes/private.txt" not in out          # not exported: not shown
    assert "src/new.py |" not in out               # new paths are listed as added instead
    assert "+ src/new.py" in out


def test_diff_shows_full_content_of_fixture_docs_and_evals_edits(repo, capsys, no_network):
    _published_then_edited(repo)
    capsys.readouterr()
    assert cli.diff(repo, ref="HEAD") == 0
    out = capsys.readouterr().out
    full = out.split("full diff: 1 fixture/evals/docs file(s)", 1)[1]
    assert "+pasted corpus line" in full and "b/src/fixtures/notes.txt" in full
    assert "src/a.py" not in full                  # code edits: the stat line only


def test_diff_content_output_is_deterministic(repo, capsys, no_network, monkeypatch):
    _published_then_edited(repo)
    _git(repo, "config", "diff.algorithm", "patience")    # repo config must not matter
    _git(repo, "config", "diff.noprefix", "true")
    capsys.readouterr()
    cli.diff(repo, ref="HEAD")
    first = capsys.readouterr().out
    monkeypatch.setenv("COLUMNS", "20")
    cli.diff(repo, ref="HEAD")
    assert capsys.readouterr().out == first
    assert "a/src/fixtures/notes.txt" in first


def test_diff_full_content_is_capped(repo, capsys, no_network, monkeypatch):
    _published_then_edited(repo)
    monkeypatch.setattr(cli, "_FULL_DIFF_MAX_LINES", 3)
    capsys.readouterr()
    assert cli.diff(repo, ref="HEAD") == 0
    assert "(truncated:" in capsys.readouterr().out


def test_diff_first_publish_has_no_content_diff(repo, capsys, no_network):
    assert cli.diff(repo, ref="HEAD") == 0
    assert "content: first publish" in capsys.readouterr().out


@pytest.mark.parametrize("rel,full", [
    ("evals/retrieval/golden.jsonl", True),
    ("docs/setup/README.md", True),
    ("KB_Core/docs/PRINCIPLES.md", True),
    ("README.md", True),
    ("KB_Core/tests/fixtures/corpus.json", True),
    ("KB_Core/tests/fixture_notes.py", True),
    ("KB_Core/src/kb_core/db.py", False),
    ("KB_Core/tests/test_db.py", False),
])
def test_full_diff_selection(rel, full):
    assert cli._wants_full_diff(rel) is full
