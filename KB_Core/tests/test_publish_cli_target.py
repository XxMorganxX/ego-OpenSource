"""kb-publish --push validates its target before committing anything.

The private repo here has a github.com `origin`; the public mirror is a local
bare repo reached as ssh://git@github.com/acme/mirror.git through a fake
GIT_SSH_COMMAND, and GitHub itself is the FakeGitHub seam.
"""
import os
import subprocess
from datetime import datetime, timezone

import pytest

from kb_core.publish import cli, git_ops, lock, state
from tests.publish_fakes import PRIVATE_URL, PUBLIC, PUBLIC_ID, PUBLIC_URL, gated, wire_public

MANIFEST = """
[publish]
remote = "/nonexistent/kb-publish-placeholder.git"
[export]
include = ["src/**", "publish.manifest.toml"]
"""
DENY = '[deny]\npatterns = ["secretname"]\n'
MIRROR = ("ego-OS mirror", "mirror@invalid")


@pytest.fixture(autouse=True)
def _mirror_identity(monkeypatch):
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_NAME", MIRROR[0])
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_EMAIL", MIRROR[1])


def _git(root, *args, env=None):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, env=env).stdout


def _repo(tmp_path, monkeypatch, *, pin=True, commit_env=None):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "publish.manifest.toml").write_text(MANIFEST)
    (root / "publish.deny.toml").write_text(DENY)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init", env=commit_env)
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    _git(root, "add", lock.LOCK_REL)
    _git(root, "commit", "-qm", "approve", env=commit_env)
    remote, gh = wire_public(tmp_path, root, monkeypatch, pin=pin)
    return root, remote, gh


def _push(root, gh, remote=PUBLIC_URL, **kw):
    # gated(): CI green, on origin/main, sha + gate changes acknowledged — so
    # each refusal below is the target check's, not the pull-side gate's.
    return cli.run(root, ref="HEAD", push=True, tag=kw.pop("tag", None),
                   remote=remote, github=gh, **gated(root), **kw)


def _main(remote):
    r = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/heads/main"],
                       cwd=remote, capture_output=True, text=True)
    return r.stdout.strip() or None


def test_push_to_pinned_public_repo(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh) == 0
    out = capsys.readouterr().out
    assert f"target = {PUBLIC} (id {PUBLIC_ID}, public" in out
    st = state.load(root)
    assert st["last_published_public_sha"] == _main(remote)
    assert st["last_published_ref"] == _git(root, "rev-parse", "HEAD").strip()
    assert st["public_repo_id"] == PUBLIC_ID               # the pin survives a push


def test_second_push_extends_sync_history(tmp_path, monkeypatch):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh) == 0
    first = _main(remote)
    (root / "src" / "a.py").write_text("a = 2\n")
    _git(root, "commit", "-qam", "edit")
    assert _push(root, gh) == 0
    assert _git(remote, "rev-parse", "main^").strip() == first


def test_push_without_pin_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch, pin=False)
    assert _push(root, gh) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "kb-publish pin" in out
    assert _main(remote) is None


@pytest.mark.parametrize("url", [PRIVATE_URL, "https://github.com/acme/private-repo"])
def test_push_to_private_origin_refused(tmp_path, monkeypatch, capsys, url):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh, remote=url) == 1
    assert "private origin" in capsys.readouterr().out


def test_push_to_local_path_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh, remote=str(remote)) == 1
    assert "not a github.com" in capsys.readouterr().out
    assert _main(remote) is None


@pytest.mark.parametrize("change,needle", [
    ({"visibility": "private"}, "not public"),
    ({"id": 4242}, "pinned"),
])
def test_push_target_mismatch_refused_before_any_git_work(tmp_path, monkeypatch, capsys,
                                                          change, needle):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    gh.set(PUBLIC, **change)

    def _never(*a, **kw):
        raise AssertionError("publish_tree must not run for an invalid target")

    monkeypatch.setattr(cli.git_ops, "publish_tree", _never)
    assert _push(root, gh) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and needle in out


def test_github_lookup_failure_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    del gh.repos[PUBLIC]
    assert _push(root, gh) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert _main(remote) is None


def test_foreign_public_commit_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh) == 0
    work = tmp_path / "web"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    (work / "README.md").write_text("web edit\n")
    _git(work, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Web Person", "GIT_AUTHOR_EMAIL": "web@example.com",
           "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "web-flow@users.noreply.github.com"}
    _git(work, "commit", "-qm", "Clarify README", env=env)
    _git(work, "push", "-q", "origin", "main")
    foreign = _main(remote)
    gh.set(PUBLIC, empty=False)
    capsys.readouterr()
    assert _push(root, gh) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "Clarify README" in out and foreign[:12] in out
    assert _main(remote) == foreign


def test_dry_run_needs_no_pin_and_no_github(tmp_path, monkeypatch):
    root, remote, gh = _repo(tmp_path, monkeypatch, pin=False)
    assert cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True,
                   remote=PUBLIC_URL, github=gh) == 0
    assert gh.calls == [] and _main(remote) is None


def test_commit_dated_to_upstream_utc_day(tmp_path, monkeypatch, capsys):
    # 23:30 at UTC-5 is already the next day in UTC: both the message and the
    # pinned commit date use the UTC calendar day, at 00:00:00 +0000.
    env = {**os.environ, "GIT_COMMITTER_DATE": "2026-10-14T23:30:00-0500",
           "GIT_AUTHOR_DATE": "2026-10-14T23:30:00-0500"}
    root, remote, gh = _repo(tmp_path, monkeypatch, commit_env=env)
    assert _push(root, gh) == 0
    raw = _git(remote, "cat-file", "commit", "main")
    epoch = int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
    assert f"committer {MIRROR[0]} <{MIRROR[1]}> {epoch} +0000" in raw
    assert "sync: 2026-10-15 (upstream " in raw


def test_push_failure_is_a_refusal(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)

    def _refuse(*a, **kw):
        raise git_ops.PublishRefused("push to x failed: rejected")

    monkeypatch.setattr(cli.git_ops, "publish_tree", _refuse)
    assert _push(root, gh) == 1
    assert "REFUSED — push to x failed" in capsys.readouterr().out
    assert state.load(root)["last_published_public_sha"] is None
