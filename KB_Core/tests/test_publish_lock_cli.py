"""The lockfile wired into kb-publish, `kb-publish approve` and the strict guard.

A new nested file used to export silently ("assembled N files"). Now a
path that was never approved, or a pinned file whose bytes changed, is
refused — and the diff is printed every run — until `kb-publish approve` has
shown it to a human and the lockfile is committed at the ref being published.
"""
import io
import subprocess

import pytest

from kb_core.publish import check, cli, lock
from tests.publish_fakes import ensure_origin, gated

MANIFEST = """
[publish]
remote = "{remote}"
[export]
include = ["src/**", "publish.manifest.toml"]
# non-UTF-8 needs this (a gate file) AND a pin
allow_binary = ["src/odd.txt"]
"""
DENY = '[deny]\npatterns = ["secretname"]\n'


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout


def _commit(root, *paths, msg="c"):
    _git(root, "add", "-A", "--", *paths)
    _git(root, "commit", "-qm", msg)


def _write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())


def _repo(tmp_path, *, approved=True):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    remote = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _write(root, "publish.manifest.toml", MANIFEST.format(remote=remote))
    _write(root, "publish.deny.toml", DENY)
    _write(root, "src/a.py", "a = 1\n")
    _write(root, "src/b.py", "b = 2\n")
    _write(root, "src/rows.jsonl", '{"q": "x"}\n')
    _commit(root, ".", msg="init")
    if approved:
        assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
        _commit(root, lock.LOCK_REL, msg="approve")
    return root, remote


def _gated_push(root, remote):
    """A push that clears the pull-side gate, so the lockfile is what refuses."""
    ensure_origin(root)
    return cli.run(root, ref="HEAD", push=True, tag=None, remote=str(remote), **gated(root))


def _dry(root):
    return cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True)


# --- approve -----------------------------------------------------------------

def test_approve_writes_lockfile_with_heads(tmp_path, capsys):
    root, _ = _repo(tmp_path, approved=False)
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    out = capsys.readouterr().out
    for rel in ("src/a.py", "src/b.py", "src/rows.jsonl", "publish.manifest.toml"):
        assert rel in out
    assert "bytes" in out and "| a = 1" in out
    got = lock.load(root)
    assert set(got) == {"src/a.py", "src/b.py", "src/rows.jsonl", "publish.manifest.toml"}
    assert got["src/a.py"] is None
    assert got["src/rows.jsonl"] == lock.blob_hash(b'{"q": "x"}\n')
    assert got["publish.manifest.toml"] is not None


def test_approve_noninteractive_refused(tmp_path, capsys, monkeypatch):
    # No terminal, no approval. There is no --yes to get around it.
    root, _ = _repo(tmp_path, approved=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))   # piped "y": still not a terminal
    assert cli.approve(root, ref="HEAD") == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "not a terminal" in out and "--yes" not in out
    assert not (root / lock.LOCK_REL).exists()


def test_approve_declined_writes_nothing(tmp_path, capsys):
    root, _ = _repo(tmp_path, approved=False)
    assert cli.approve(root, ref="HEAD", confirm=lambda prompt: False) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert not (root / lock.LOCK_REL).exists()


def test_approve_confirmed_writes(tmp_path):
    root, _ = _repo(tmp_path, approved=False)
    prompts = []
    assert cli.approve(root, ref="HEAD",
                       confirm=lambda p: prompts.append(p) or True) == 0
    assert prompts and "[y/N]" in prompts[0]
    assert lock.load(root) is not None


def test_approve_shows_changed_and_removed(tmp_path, capsys):
    root, _ = _repo(tmp_path)
    _write(root, "src/rows.jsonl", '{"q": "y"}\n')
    (root / "src" / "b.py").unlink()
    _commit(root, ".", msg="edit")
    capsys.readouterr()
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    out = capsys.readouterr().out
    assert "~ src/rows.jsonl" in out and '| {"q": "y"}' in out
    assert "- src/b.py" in out
    got = lock.load(root)
    assert "src/b.py" not in got
    assert got["src/rows.jsonl"] == lock.blob_hash(b'{"q": "y"}\n')


def test_approve_up_to_date_needs_no_confirmation(tmp_path, capsys):
    root, _ = _repo(tmp_path)
    capsys.readouterr()
    assert cli.approve(root, ref="HEAD", confirm=lambda p: False) == 0
    assert "up to date" in capsys.readouterr().out


def test_approve_binary_shown_as_binary(tmp_path, capsys):
    root, _ = _repo(tmp_path)
    _write(root, "src/odd.txt", "secret".encode("utf-16"))
    _commit(root, ".", msg="odd")
    capsys.readouterr()
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    out = capsys.readouterr().out
    assert "+ src/odd.txt" in out and "binary" in out
    assert lock.load(root)["src/odd.txt"] == lock.blob_hash((root / "src/odd.txt").read_bytes())


def test_approve_refused_without_deny_file(tmp_path, capsys):
    root, _ = _repo(tmp_path, approved=False)
    (root / "publish.deny.toml").unlink()
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert not (root / lock.LOCK_REL).exists()


def test_main_approve_subcommand(tmp_path, monkeypatch):
    root, _ = _repo(tmp_path, approved=False)
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["approve", "--ref", "HEAD"]) == 0
    assert lock.load(root) is not None


def test_main_approve_has_no_yes_flag(tmp_path, monkeypatch):
    root, _ = _repo(tmp_path, approved=False)
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit):
        cli.main(["approve", "--ref", "HEAD", "--yes"])
    assert not (root / lock.LOCK_REL).exists()


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_push_cannot_auto_approve(tmp_path, monkeypatch):
    # There is no way to say "approve and push", and no --yes anywhere.
    root, _ = _repo(tmp_path)
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit):
        cli.main(["--push", "--yes"])


# --- kb-publish --------------------------------------------------------------

def test_run_prints_diff_every_run(tmp_path, capsys):
    root, _ = _repo(tmp_path)
    assert _dry(root) == 0
    out = capsys.readouterr().out
    assert "lockfile" in out and "0 new" in out and "0 removed" in out


def test_run_refuses_without_lockfile_at_ref(tmp_path, capsys):
    root, remote = _repo(tmp_path, approved=False)
    assert _gated_push(root, remote) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "kb-publish approve" in out
    assert subprocess.run(["git", "show-ref"], cwd=remote, capture_output=True,
                          text=True).stdout.strip() == ""


def test_run_refuses_new_path_and_prints_it(tmp_path, capsys):
    root, remote = _repo(tmp_path)
    _write(root, "src/deep/new_notes.md", "clean\n")
    _commit(root, ".", msg="new")
    assert _gated_push(root, remote) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "+ src/deep/new_notes.md" in out
    assert subprocess.run(["git", "show-ref"], cwd=remote, capture_output=True,
                          text=True).stdout.strip() == ""


def test_run_refuses_changed_pinned(tmp_path, capsys):
    root, _ = _repo(tmp_path)
    _write(root, "src/rows.jsonl", '{"q": "real row"}\n')
    _commit(root, ".", msg="data")
    assert _dry(root) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "~ src/rows.jsonl" in out


def test_run_unpinned_edit_passes(tmp_path):
    root, _ = _repo(tmp_path)
    _write(root, "src/a.py", "a = 99\n")
    _commit(root, ".", msg="code")
    assert _dry(root) == 0


def test_run_reports_removed_but_passes(tmp_path, capsys):
    root, _ = _repo(tmp_path)
    (root / "src" / "b.py").unlink()
    _commit(root, ".", msg="rm")
    assert _dry(root) == 0
    assert "- src/b.py" in capsys.readouterr().out


def test_run_uncommitted_approval_does_not_count(tmp_path, capsys):
    # The approval that counts is the one committed at the ref being published.
    root, _ = _repo(tmp_path)
    _write(root, "src/new.py", "n = 1\n")
    _commit(root, "src/new.py", msg="new")
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    capsys.readouterr()
    assert _dry(root) == 1
    out = capsys.readouterr().out
    assert "+ src/new.py" in out and "commit" in out.lower()
    _commit(root, lock.LOCK_REL, msg="approve new")
    assert _dry(root) == 0


def test_run_pinned_non_utf8_passes_until_it_changes(tmp_path, capsys):
    root, _ = _repo(tmp_path)
    _write(root, "src/odd.txt", "fine".encode("utf-16"))
    _commit(root, ".", msg="odd")
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    _commit(root, lock.LOCK_REL, msg="approve odd")
    assert _dry(root) == 0                      # the pin exempts it from the UTF-8 rule
    _write(root, "src/odd.txt", "changed".encode("utf-16"))
    _commit(root, ".", msg="odd2")
    capsys.readouterr()
    assert _dry(root) == 1
    out = capsys.readouterr().out
    assert "~ src/odd.txt" in out and "not strict UTF-8" in out


def test_push_never_writes_the_lockfile(tmp_path):
    root, remote = _repo(tmp_path)
    before = (root / lock.LOCK_REL).read_bytes()
    _write(root, "src/new.py", "n = 1\n")
    _commit(root, "src/new.py", msg="new")
    assert _gated_push(root, remote) == 1
    assert (root / lock.LOCK_REL).read_bytes() == before


def test_run_malformed_lockfile_at_ref_refused(tmp_path, capsys):
    root, _ = _repo(tmp_path)
    (root / lock.LOCK_REL).write_text("src/a.py\tnot-a-hash\n")
    _commit(root, lock.LOCK_REL, msg="break")
    assert _dry(root) == 1
    assert "REFUSED" in capsys.readouterr().out


# --- kb-check-publishable ----------------------------------------------------

@pytest.fixture
def guard_env(monkeypatch):
    monkeypatch.setenv("PUBLISH_DENY_CANARIES", "secretname")
    monkeypatch.delenv("KB_PUBLISH_GUARD_REQUIRED", raising=False)


def _check(root, monkeypatch, *argv):
    monkeypatch.chdir(root)
    return check.main(list(argv))


STRICT = ("--strict", "--min-selected", "1")


def test_strict_without_lockfile_refused(tmp_path, monkeypatch, capsys, guard_env):
    root, _ = _repo(tmp_path, approved=False)
    assert _check(root, monkeypatch, *STRICT) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and lock.LOCK_REL in out


def test_strict_approved_tree_passes(tmp_path, monkeypatch, capsys, guard_env):
    root, _ = _repo(tmp_path)
    assert _check(root, monkeypatch, *STRICT) == 0


def test_strict_new_path_refused_and_printed(tmp_path, monkeypatch, capsys, guard_env):
    root, _ = _repo(tmp_path)
    _write(root, "src/notes_ish/n.md", "clean\n")
    _git(root, "add", "-A")
    assert _check(root, monkeypatch, *STRICT) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "+ src/notes_ish/n.md" in out


def test_strict_changed_pinned_refused(tmp_path, monkeypatch, capsys, guard_env):
    root, _ = _repo(tmp_path)
    _write(root, "src/rows.jsonl", '{"q": "z"}\n')
    assert _check(root, monkeypatch, *STRICT) == 1
    assert "~ src/rows.jsonl" in capsys.readouterr().out


def test_strict_removed_only_passes_and_reports(tmp_path, monkeypatch, capsys, guard_env):
    root, _ = _repo(tmp_path)
    _git(root, "rm", "-q", "src/b.py")
    assert _check(root, monkeypatch, *STRICT) == 0
    assert "- src/b.py" in capsys.readouterr().out


def test_strict_pinned_non_utf8_passes(tmp_path, monkeypatch, guard_env):
    root, _ = _repo(tmp_path)
    _write(root, "src/odd.txt", "fine".encode("utf-16"))
    _commit(root, ".", msg="odd")
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    assert _check(root, monkeypatch, *STRICT) == 0


def test_nonstrict_new_path_is_a_note(tmp_path, monkeypatch, capsys, guard_env):
    # The day-to-day CI job isn't the publish gate; it says what kb-publish will refuse.
    root, _ = _repo(tmp_path)
    _write(root, "src/new.py", "n = 1\n")
    _git(root, "add", "-A")
    assert _check(root, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "+ src/new.py" in out and "kb-publish" in out


@pytest.mark.parametrize("strict", [True, False])
def test_malformed_lockfile_refused(tmp_path, monkeypatch, capsys, guard_env, strict):
    root, _ = _repo(tmp_path)
    (root / lock.LOCK_REL).write_text("src/a.py\tzz\n")
    argv = STRICT if strict else ()
    assert _check(root, monkeypatch, *argv) == 1
    assert "REFUSED" in capsys.readouterr().out
