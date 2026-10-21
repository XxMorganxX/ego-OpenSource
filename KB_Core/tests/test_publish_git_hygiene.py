"""publish_tree commit hygiene and remote-history checks.

Every remote here is a local bare repo (or one served through a fake
GIT_SSH_COMMAND); nothing reaches the network. The shell environment and the
global git config are deliberately hostile — identity env vars, signing,
hooks, URL rewrites — because the sync commit must come out byte-for-byte the
same regardless of who runs kb-publish on which machine.
"""
import itertools
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from kb_core.publish import git_ops
from kb_core.publish.target import Target
from tests.publish_fakes import PUBLIC, PUBLIC_ID, PUBLIC_URL, bare, fake_ssh_command

MIRROR = ("ego-OS mirror", "mirror@invalid")
PATTERNS = [re.compile("secretname", re.IGNORECASE)]
EMPTY = Target(full_name=PUBLIC, repo_id=PUBLIC_ID, empty=True)
NON_EMPTY = Target(full_name=PUBLIC, repo_id=PUBLIC_ID, empty=False)
DATE = "2026-10-14"
EPOCH = int(datetime(2026, 10, 14, tzinfo=timezone.utc).timestamp())
_n = itertools.count()


@pytest.fixture(autouse=True)
def _isolated_git_home(tmp_path, monkeypatch):
    """A neutral HOME so the developer's own ~/.gitconfig never leaks into a test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=env).stdout


def _tree(tmp_path):
    t = tmp_path / f"tree{next(_n)}"
    (t / "src").mkdir(parents=True)
    (t / "src" / "a.py").write_text("a = 1\n")
    (t / "README.md").write_text("hello\n")
    return t


def _remote(tmp_path):
    return bare(tmp_path / "gh")


def _publish(tree, remote, tmp_path, **kw):
    args = dict(message=f"sync: {DATE} (upstream abc1234)", coauthors=[], push=True,
                tag=None, workdir=tmp_path / f"w{next(_n)}", deny_patterns=PATTERNS,
                identity=MIRROR, target=EMPTY, date=DATE)
    args.update(kw)
    return git_ops.publish_tree(tree, str(remote), **args)


def _refs(remote):
    return subprocess.run(["git", "show-ref"], cwd=remote, capture_output=True,
                          text=True).stdout


def _main(remote):
    r = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/heads/main"],
                       cwd=remote, capture_output=True, text=True)
    return r.stdout.strip() or None


def _foreign_commit(remote, tmp_path, *, subject, author, committer=None, merge=False):
    """A commit made on public main by someone other than kb-publish (web UI, a merge)."""
    work = tmp_path / f"foreign{next(_n)}"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    committer = committer or author
    env = {**os.environ,
           "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
           "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
    (work / "README.md").write_text("edited on the web\n")
    _git(work, "add", "-A")
    if merge:
        _git(work, "checkout", "-q", "-b", "side")
        _git(work, "commit", "-qm", "side", env=env)
        _git(work, "checkout", "-q", "main")
        _git(work, "merge", "-q", "--no-ff", "-m", subject, "side", env=env)
    else:
        _git(work, "commit", "-qm", subject, env=env)
    _git(work, "push", "-q", "origin", "main")
    return _git(work, "rev-parse", "HEAD").strip()


# --- target is required for a push -------------------------------------------

def test_push_requires_a_validated_target(tmp_path):
    remote = _remote(tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="target"):
        _publish(_tree(tmp_path), remote, tmp_path, target=None)
    assert _main(remote) is None


def test_dry_run_needs_no_target(tmp_path):
    remote = _remote(tmp_path)
    _publish(_tree(tmp_path), remote, tmp_path, target=None, push=False)
    assert _main(remote) is None


def test_empty_deny_patterns_refused(tmp_path):
    remote = _remote(tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="deny"):
        _publish(_tree(tmp_path), remote, tmp_path, deny_patterns=[])
    assert _main(remote) is None


# --- remote history: empty, or sync:-only by the mirror identity -------------

def test_sync_only_history_is_extended(tmp_path):
    remote, tree = _remote(tmp_path), _tree(tmp_path)
    first = _publish(tree, remote, tmp_path)
    second = _publish(tree, remote, tmp_path, target=NON_EMPTY, last_public_sha=first,
                      message=f"sync: {DATE} (upstream abc1235)")
    assert _main(remote) == second
    assert _git(remote, "rev-parse", f"{second}^").strip() == first


@pytest.mark.parametrize("subject,author,committer,merge", [
    ("Clarify README", ("Web Person", "web@example.com"), ("GitHub", "web-flow@users.noreply.github.com"), False),
    ("fix typo", MIRROR, None, False),                                  # mirror, not sync:
    ("sync: looks legit", ("Web Person", "web@example.com"), None, False),
    ("sync: web edit", MIRROR, ("GitHub", "web-flow@users.noreply.github.com"), False),
    ("sync: merged", MIRROR, None, True),                               # a merge
])
def test_foreign_public_commit_refused_and_listed(tmp_path, subject, author, committer, merge):
    remote, tree = _remote(tmp_path), _tree(tmp_path)
    _publish(tree, remote, tmp_path)
    foreign = _foreign_commit(remote, tmp_path, subject=subject, author=author,
                              committer=committer, merge=merge)
    with pytest.raises(git_ops.PublishRefused) as e:
        _publish(tree, remote, tmp_path, target=NON_EMPTY)
    assert foreign[:7] in str(e.value) and subject in str(e.value)
    assert _main(remote) == foreign                     # nothing was pushed over it


def test_changing_the_mirror_identity_makes_old_syncs_foreign(tmp_path):
    remote, tree = _remote(tmp_path), _tree(tmp_path)
    _publish(tree, remote, tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="not a sync"):
        _publish(tree, remote, tmp_path, target=NON_EMPTY,
                 identity=("other mirror", "other@invalid"))


def test_remote_with_branches_but_no_main_refused(tmp_path):
    remote, tree = _remote(tmp_path), _tree(tmp_path)
    work = tmp_path / "w-dev"
    _git(tmp_path, "init", "-q", str(work))
    (work / "x").write_text("x\n")
    _git(work, "add", "x")
    _git(work, "-c", "user.name=X", "-c", "user.email=x@example.com", "commit", "-qm", "dev")
    _git(work, "push", "-q", str(remote), "HEAD:refs/heads/dev")
    with pytest.raises(git_ops.PublishRefused, match="no main"):
        _publish(tree, remote, tmp_path, target=NON_EMPTY)
    assert _main(remote) is None


def test_last_published_public_sha_must_still_be_in_history(tmp_path):
    remote, tree = _remote(tmp_path), _tree(tmp_path)
    first = _publish(tree, remote, tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="last published"):
        _publish(tree, remote, tmp_path, target=NON_EMPTY, last_public_sha="1" * 40,
                 message=f"sync: {DATE} (upstream abc1235)")
    assert _main(remote) == first


def test_empty_remote_with_a_recorded_publish_refused(tmp_path):
    remote, tree = _remote(tmp_path), _tree(tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="last published"):
        _publish(tree, remote, tmp_path, last_public_sha="1" * 40)
    assert _main(remote) is None


# --- clone failure: no "assume empty" for a push unless GitHub says empty -----

def test_clone_failure_refused_for_push_when_repo_not_empty(tmp_path):
    missing = tmp_path / "missing.git"
    with pytest.raises(git_ops.PublishRefused, match="clone"):
        _publish(_tree(tmp_path), missing, tmp_path, target=NON_EMPTY)
    assert not missing.exists()


def test_clone_failure_tolerated_for_push_only_when_github_reports_empty(tmp_path, capsys):
    missing = tmp_path / "missing.git"
    # Gets past the clone (GitHub says empty), then the push itself fails — and
    # that failure is a refusal, not a traceback.
    with pytest.raises(git_ops.PublishRefused, match="push"):
        _publish(_tree(tmp_path), missing, tmp_path, target=EMPTY)
    assert "reports" in capsys.readouterr().out


# --- scrubbed environment ------------------------------------------------------

def _hostile_global_config(home, tmp_path):
    hooks = tmp_path / "global-hooks"
    hooks.mkdir()
    for name, body in {
        "commit-msg": 'echo "Signed-off-by: Real Person <real.person@corp.example>" >> "$1"',
        "prepare-commit-msg": 'echo "prepared by a hook" >> "$1"',
        "post-checkout": "echo stray > stray-from-hook.txt",
        "pre-push": "exit 1",
    }.items():
        p = hooks / name
        p.write_text(f"#!/bin/sh\n{body}\n")
        p.chmod(0o755)
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Real Person\n\temail = real.person@corp.example\n"
        "[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n\tforceSignAnnotated = true\n"
        "[gpg]\n\tprogram = /nonexistent/gpg\n"
        "[i18n]\n\tcommitEncoding = ISO-8859-1\n"
        f"[core]\n\thooksPath = {hooks}\n")


def test_commit_and_tag_ignore_env_and_global_config(tmp_path, monkeypatch, _isolated_git_home):
    _hostile_global_config(_isolated_git_home, tmp_path)
    remote, tree = _remote(tmp_path), _tree(tmp_path)
    decoy = bare(tmp_path / "decoy")       # an inherited GIT_DIR must not receive anything
    for k, v in {
        "GIT_AUTHOR_NAME": "Env Author", "GIT_AUTHOR_EMAIL": "env.author@corp.example",
        "GIT_AUTHOR_DATE": "2001-02-03T04:05:06+0700",
        "GIT_COMMITTER_NAME": "Env Committer", "GIT_COMMITTER_EMAIL": "env.c@corp.example",
        "GIT_COMMITTER_DATE": "2001-02-03T04:05:06+0700",
        "EMAIL": "env.email@corp.example", "TZ": "Etc/GMT+5",
        "GIT_CONFIG_PARAMETERS": "'user.name'='Param Person'",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.email",
        "GIT_CONFIG_VALUE_0": "param@corp.example",
        "GIT_DIR": str(decoy), "GIT_INDEX_FILE": str(tmp_path / "decoy-index"),
    }.items():
        monkeypatch.setenv(k, v)
    sha = _publish(tree, remote, tmp_path, tag="v0.1.0",
                   coauthors=[{"name": "Pat", "email": "pat@example.com"}])
    for k in ("GIT_DIR", "GIT_INDEX_FILE"):
        monkeypatch.delenv(k)
    assert _refs(decoy) == "" and not (tmp_path / "decoy-index").exists()

    ident = f"ego-OS mirror <mirror@invalid> {EPOCH} +0000"
    commit = _git(remote, "cat-file", "commit", sha)
    headers, _, body = commit.partition("\n\n")
    assert [h.split(" ", 1)[0] for h in headers.splitlines()] == ["tree", "author", "committer"]
    assert f"author {ident}" in headers and f"committer {ident}" in headers
    assert body == (f"sync: {DATE} (upstream abc1234)\n\n"
                    "Co-authored-by: Pat <pat@example.com>")
    tag = _git(remote, "cat-file", "-p", "refs/tags/v0.1.0")
    assert f"object {sha}" in tag and f"tagger {ident}" in tag
    assert "BEGIN PGP" not in tag
    assert _git(remote, "ls-tree", "-r", "--name-only", sha).split() == ["README.md", "src/a.py"]
    everything = _git(remote, "log", "--all", "--format=raw")
    assert "corp.example" not in everything and "Person" not in everything


def test_date_must_be_iso(tmp_path):
    remote = _remote(tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="date"):
        _publish(_tree(tmp_path), remote, tmp_path, date="14/10/2026")
    assert _main(remote) is None


def test_default_date_is_today_utc_midnight(tmp_path):
    remote = _remote(tmp_path)
    sha = _publish(_tree(tmp_path), remote, tmp_path, date=None)
    stamp = _git(remote, "log", "-1", "--format=%at %ad", "--date=raw", sha).split()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    assert int(stamp[0]) in {int(today.timestamp()), int((today - timedelta(days=1)).timestamp())}
    assert stamp[-1] == "+0000"


@pytest.mark.parametrize("identity", [("Mirror <x>", "m@invalid"), ("Mirror", "m\n@invalid"),
                                      ("", "m@invalid"), ("Mirror", "")])
def test_malformed_identity_refused(tmp_path, identity):
    remote = _remote(tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="identity"):
        _publish(_tree(tmp_path), remote, tmp_path, identity=identity)
    assert _main(remote) is None


@pytest.mark.parametrize("tag", ["-d", "v 1", "v1..2", "refs/heads/main", "v1.lock", ""])
def test_malformed_tag_refused(tmp_path, tag):
    remote = _remote(tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="tag"):
        _publish(_tree(tmp_path), remote, tmp_path, tag=tag)
    assert _main(remote) is None


# --- post-commit object scan ------------------------------------------------

@pytest.mark.parametrize("kw", [
    {"message": "sync: secretname"},
    {"coauthors": [{"name": "Pat", "email": "secretname@example.com"}]},
    {"tag": "secretname-v1"},
])
def test_deny_hit_in_commit_or_tag_object_refused(tmp_path, kw):
    remote = _remote(tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="object"):
        _publish(_tree(tmp_path), remote, tmp_path, **kw)
    assert _main(remote) is None and "secretname" not in _refs(remote)


def test_identity_hit_in_object_refused_unless_allowed(tmp_path):
    remote, tree = _remote(tmp_path), _tree(tmp_path)
    ident = ("secretname bot", "mirror@invalid")
    with pytest.raises(git_ops.PublishRefused, match="object"):
        _publish(tree, remote, tmp_path, identity=ident)
    assert _main(remote) is None
    _publish(tree, remote, tmp_path, identity=ident, allow_identity_hit=True)
    assert _main(remote) is not None


def test_allowed_identity_hit_does_not_excuse_the_message(tmp_path):
    remote = _remote(tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="object"):
        _publish(_tree(tmp_path), remote, tmp_path, identity=("secretname bot", "m@invalid"),
                 allow_identity_hit=True, message="sync: secretname")
    assert _main(remote) is None


# --- the committed tree is exactly the export tree ----------------------------

def test_export_bytes_committed_verbatim(tmp_path):
    # In-tree .gitattributes/.gitignore must not rewrite or drop exported bytes:
    # what was scanned and approved is what gets published.
    remote, tree = _remote(tmp_path), _tree(tmp_path)
    (tree / ".gitattributes").write_text("* text eol=crlf\n")
    (tree / ".gitignore").write_text("*.log\n")
    (tree / "keep.log").write_text("kept\n")
    (tree / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    (tree / "run.sh").write_text("#!/bin/sh\n")
    (tree / "run.sh").chmod(0o755)
    sha = _publish(tree, remote, tmp_path)
    listing = _git(remote, "ls-tree", "-r", sha)
    assert "keep.log" in listing
    assert re.search(r"^100755 blob \w+\trun\.sh$", listing, re.M)
    out = subprocess.run(["git", "cat-file", "blob", f"{sha}:crlf.txt"], cwd=remote,
                         capture_output=True, check=True).stdout
    assert out == b"a\r\nb\r\n"


# --- transport: ssh:// with GIT_SSH_COMMAND (deploy key), no URL rewriting -------

def test_ssh_remote_pushes_through_git_ssh_command(tmp_path, monkeypatch):
    root = tmp_path / "gh"
    remote = bare(root)
    monkeypatch.setenv("GIT_SSH_COMMAND", fake_ssh_command(tmp_path, root))
    sha = git_ops.publish_tree(
        _tree(tmp_path), PUBLIC_URL, message="sync: ssh", coauthors=[], push=True,
        tag="v0.1.0", workdir=tmp_path / "wssh", deny_patterns=PATTERNS, identity=MIRROR,
        target=EMPTY, date=DATE)
    assert _main(remote) == sha
    assert "refs/tags/v0.1.0" in _refs(remote)


@pytest.mark.parametrize("key", ["insteadOf", "pushInsteadOf"])
def test_url_rewrite_in_global_config_refused(tmp_path, monkeypatch, _isolated_git_home, key):
    root = tmp_path / "gh"
    remote, other = bare(root), bare(root, "acme/somewhere-else")
    monkeypatch.setenv("GIT_SSH_COMMAND", fake_ssh_command(tmp_path, root))
    (_isolated_git_home / ".gitconfig").write_text(
        f'[url "ssh://git@github.com/acme/somewhere-else.git"]\n\t{key} = {PUBLIC_URL}\n')
    with pytest.raises(git_ops.PublishRefused, match="rewrit"):
        git_ops.publish_tree(
            _tree(tmp_path), PUBLIC_URL, message="sync: x", coauthors=[], push=True,
            tag=None, workdir=tmp_path / "wrw", deny_patterns=PATTERNS, identity=MIRROR,
            target=EMPTY, date=DATE)
    assert _main(remote) is None and _main(other) is None


def test_allowed_identity_hit_covers_header_lines_only(tmp_path):
    # A body line that merely starts with "author" is message text, not the
    # verified identity header — --allow-identity-hit must not excuse it.
    remote = _remote(tmp_path)
    with pytest.raises(git_ops.PublishRefused, match="object"):
        _publish(_tree(tmp_path), remote, tmp_path, identity=("secretname bot", "m@invalid"),
                 allow_identity_hit=True, message="sync: x\n\nauthor secretname")
    assert _main(remote) is None
