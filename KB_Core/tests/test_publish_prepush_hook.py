"""The allowlist pre-push hook and its installer.

Only the private repo is ever a push target from a dev clone; publishing goes
through kb-publish's own temp clone. The hook and installer live under
private/githooks/ — private by construction, never exported. In the private
repo (it has publish.deny.toml) they MUST exist and these tests run; in the
exported tree there is nothing to test.

The private repo's slug is read from the hook itself, never typed here: this
file is exported, and the slug is a denylisted identifier.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "private" / "githooks"

if not (ROOT / "publish.deny.toml").exists():
    pytest.skip("exported tree: private/githooks is private-only", allow_module_level=True)

HOOK = HOOKS / "pre-push"
INSTALL = HOOKS / "install.sh"


def test_hook_and_installer_exist_in_the_private_repo():
    assert HOOK.is_file() and INSTALL.is_file()
    assert os.access(HOOK, os.X_OK) and os.access(INSTALL, os.X_OK)
    for f in (HOOK, INSTALL):
        subprocess.run(["sh", "-n", str(f)], check=True)


def _allowed() -> tuple[str, str]:
    m = re.search(r'^ALLOWED="([a-z0-9._-]+)/([a-z0-9._-]+)"$', HOOK.read_text(), re.MULTILINE)
    assert m, "hook must name the private repo as ALLOWED=\"owner/name\" (lower case)"
    return m[1], m[2]


def _hook(url: str, hook: Path = HOOK) -> subprocess.CompletedProcess:
    return subprocess.run([str(hook), "origin", url], input="", capture_output=True,
                          text=True, timeout=10)


def _mixed(s: str) -> str:
    return "".join(c.upper() if i % 2 else c for i, c in enumerate(s))


def _good_urls() -> list[str]:
    o, n = _allowed()
    mo, mn = _mixed(o), n.upper()
    return [
        f"https://github.com/{o}/{n}",
        f"https://github.com/{o}/{n}.git",
        f"https://github.com/{o}/{n}.git/",
        f"https://github.com/{o}/{n}/",
        f"https://github.com/{mo}/{mn}.git",
        f"https://GitHub.com/{o}/{n}",
        f"https://x-access-token:tok123@github.com/{o}/{n}.git",
        f"https://{o}@github.com/{mo}/{n}",
        f"ssh://git@github.com/{o}/{n}.git",
        f"ssh://git@github.com:22/{o}/{n}",
        f"ssh://git@ssh.github.com:443/{mo}/{mn}.git",
        f"git@github.com:{o}/{n}.git",
        f"git@github.com:{mo}/{mn}",
        f"git@github.com:{o}/{n}.git/".upper(),
    ]


def _bad_urls() -> list[str]:
    o, n = _allowed()
    return [
        f"https://github.com/{o}/{n}-public",
        f"https://github.com/{o}/{n}x.git",
        f"https://github.com/{o}x/{n}",
        f"https://github.com/someone-else/{n}",
        f"https://github.com/{n}",
        f"https://github.com/{o}/{n}/extra",
        f"https://github.com/{o}/{n}.git.evil",
        f"https://github.com.evil.example/{o}/{n}",
        f"https://evil.example/github.com/{o}/{n}",
        f"https://gitlab.com/{o}/{n}.git",
        f"github-deploy:{o}/{n}.git",                 # ssh host alias: identity unknown
        f"git@github.com:{o}/{n}.git\nhttps://github.com/{o}/other",
        f"file:///tmp/{n}.git",
        "/tmp/public-mirror.git",
        "../mirror",
        "",
    ]


@pytest.mark.parametrize("i", range(14))
def test_private_repo_urls_pass(i):
    url = _good_urls()[i]
    r = _hook(url)
    assert r.returncode == 0, (url, r.stderr)


@pytest.mark.parametrize("i", range(16))
def test_anything_else_is_refused(i):
    url = _bad_urls()[i]
    r = _hook(url)
    assert r.returncode == 1, url
    assert "pre-push: refusing" in r.stderr and "only the private repo" in r.stderr


def test_url_lists_cover_every_case():
    assert len(_good_urls()) == 14 and len(_bad_urls()) == 16


# --- the installer -----------------------------------------------------------------

def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo carrying a copy of private/githooks, with no global hook config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    shutil.copytree(HOOKS, root / "private" / "githooks")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _install(repo: Path, *args, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(repo / "private" / "githooks" / "install.sh"), *args],
                          cwd=cwd or repo, capture_output=True, text=True, timeout=30)


def test_install_copies_into_the_common_hooks_dir(repo):
    r = _install(repo)
    assert r.returncode == 0, r.stderr
    dest = repo / ".git" / "hooks" / "pre-push"
    assert dest.read_bytes() == HOOK.read_bytes()
    assert not dest.is_symlink(), "a symlink into the checkout fails open on old branches"
    assert os.access(dest, os.X_OK)
    assert str(dest) in r.stdout


def test_install_from_a_worktree_still_targets_the_common_dir(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    r = _install(repo, cwd=wt)
    assert r.returncode == 0, r.stderr
    assert (repo / ".git" / "hooks" / "pre-push").read_bytes() == HOOK.read_bytes()
    assert not (repo / ".git" / "worktrees" / "wt" / "hooks").exists()


def test_install_is_idempotent(repo):
    assert _install(repo).returncode == 0
    r = _install(repo)
    assert r.returncode == 0 and "already installed" in r.stdout


def test_install_refuses_to_overwrite_a_different_hook(repo):
    dest = repo / ".git" / "hooks" / "pre-push"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text("#!/bin/sh\nexit 0\n")
    r = _install(repo)
    assert r.returncode != 0 and "REFUSED" in r.stderr and "--force" in r.stderr
    assert dest.read_text() == "#!/bin/sh\nexit 0\n"


def test_install_force_replaces_and_keeps_a_backup(repo):
    dest = repo / ".git" / "hooks" / "pre-push"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text("#!/bin/sh\nexit 0\n")
    r = _install(repo, "--force")
    assert r.returncode == 0, r.stderr
    assert dest.read_bytes() == HOOK.read_bytes()
    backups = list(dest.parent.glob("pre-push.bak.*"))
    assert len(backups) == 1 and backups[0].read_text() == "#!/bin/sh\nexit 0\n"
    assert str(backups[0].name) in r.stdout


def test_install_refuses_when_core_hookspath_would_bypass_it(repo):
    _git(repo, "config", "core.hooksPath", ".githooks")
    r = _install(repo)
    assert r.returncode != 0 and "REFUSED" in r.stderr and "core.hooksPath" in r.stderr
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


def test_installed_hook_blocks_a_real_push_to_anything_else(repo, tmp_path):
    bare = tmp_path / "mirror.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    assert _install(repo).returncode == 0
    r = _git(repo, "push", str(bare), "main", check=False)
    assert r.returncode != 0
    assert "pre-push: refusing" in r.stderr
    assert _git(bare, "branch", "--list").stdout.strip() == ""
