"""kb-publish checks the allowlist pre-push hook is installed and agrees with the pins.

A hook can be uninstalled; core.hooksPath (set after install, or in any
scope) silently disables it; and its hard-coded slug allows whatever repo
holds that name — after a rename, possibly a PUBLIC repo that took the old
name, while the renamed private repo is blocked. Where the checkout
carries private/githooks/pre-push (the private repo; a gate file, so removing
it is acknowledged), `kb-publish --push` now refuses unless that exact hook
is installed in the common git dir, core.hooksPath is unset, and the hook's
ALLOWED slug is the pinned private repo; `kb-publish pin` refuses a public
target the hook would allow. The hook stays advisory (--no-verify skips it):
the public repo's own push rules remain the control that actually holds.
"""
import shutil
from pathlib import Path

from kb_core.publish import cli, gate
from tests.publish_fakes import (PRIVATE, PUBLIC, _real_fetch, commit, gated_push, git,
                                 public_main, push_origin, scratch_private, seams)

ROOT = Path(__file__).resolve().parents[2]
REAL_HOOK = ROOT / "private" / "githooks" / "pre-push"
HOOK = """#!/bin/sh
# test stand-in for private/githooks/pre-push
ALLOWED="{slug}"
url=$2
norm=$(printf '%s\\n' "$url" | tr 'A-Z' 'a-z' | sed -E -e 's#^git@github\\.com:##' \\
    -e 's#^ssh://git@github\\.com/##' -e 's#\\.git$##')
[ "$norm" = "$ALLOWED" ] && exit 0
echo "pre-push: refusing $url" >&2
exit 1
"""


def _source(root, slug=PRIVATE):
    """Commit the hook source and push it to origin/main (the check reads it at
    the published sha) — before any hook is installed in this clone."""
    p = root / "private" / "githooks" / "pre-push"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(HOOK.format(slug=slug))
    p.chmod(0o755)
    commit(root, "hook")
    push_origin(root)
    return p


def _real_hook_check(root, gh):
    """gated_push with the production hook check (no missing_ok)."""
    return gated_push(root, gh, seams=seams(fetch_origin=_real_fetch,
                                            hook_problem=cli.Seams().hook_problem))


def _install(root, src):
    dest = root / ".git" / "hooks" / "pre-push"
    dest.parent.mkdir(exist_ok=True)
    shutil.copy(src, dest)
    dest.chmod(0o755)
    return dest


def test_hook_source_is_a_gate_file():
    assert gate.is_gate_file("private/githooks/pre-push")
    assert gate.is_gate_file("private/githooks/install.sh")


def test_push_refused_when_the_hook_is_not_installed(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _source(root)
    assert gated_push(root, gh) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "pre-push hook" in out and "install.sh" in out
    assert public_main(remote) is None


def test_push_with_the_hook_installed(tmp_path, monkeypatch):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _install(root, _source(root))
    assert gated_push(root, gh) == 0


def test_push_refused_when_the_installed_hook_differs(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    dest = _install(root, _source(root))
    dest.write_text("#!/bin/sh\nexit 0\n")
    assert gated_push(root, gh) == 1
    assert "differs" in capsys.readouterr().out


def test_push_refused_when_hooks_path_is_set(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _install(root, _source(root))
    git(root, "config", "core.hooksPath", ".husky")
    assert gated_push(root, gh) == 1
    assert "core.hooksPath" in capsys.readouterr().out


def test_push_refused_when_the_hook_allows_another_repo(tmp_path, monkeypatch, capsys):
    # After a rename the hook may still allow the OLD private name — now someone
    # else's, possibly public, repo.
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _install(root, _source(root, slug=PUBLIC))
    assert gated_push(root, gh) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and PUBLIC in out and "pinned private repo" in out


def test_pin_refuses_the_repo_the_hook_allows(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _source(root, slug=PUBLIC)
    assert cli.pin(root, repo=PUBLIC, github=gh) == 1
    assert "pre-push hook" in capsys.readouterr().out


def test_private_repo_without_the_hook_source_is_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert _real_hook_check(root, gh) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "missing" in out and "publish.deny.toml" in out
    assert public_main(remote) is None


def test_hook_source_is_read_from_the_published_sha(tmp_path, monkeypatch, capsys):
    """An uncommitted delete of the hook source does not switch the check off."""
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _install(root, _source(root))
    (root / "private" / "githooks" / "pre-push").unlink()
    assert _real_hook_check(root, gh) == 0


def test_uncommitted_hook_source_does_not_count(tmp_path, monkeypatch, capsys):
    """A working-tree hook the published sha does not carry is not the hook."""
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    p = root / "private" / "githooks" / "pre-push"
    p.parent.mkdir(parents=True)
    p.write_text(HOOK.format(slug=PRIVATE))
    _install(root, p)
    assert _real_hook_check(root, gh) == 1
    assert "missing" in capsys.readouterr().out


def test_installed_hook_compared_with_the_published_sha(tmp_path, monkeypatch, capsys):
    """Editing the working-tree source to match a tampered install changes nothing."""
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    src = _source(root)
    dest = _install(root, src)
    dest.write_text("#!/bin/sh\nexit 0\n")
    src.write_text("#!/bin/sh\nexit 0\n")
    assert _real_hook_check(root, gh) == 1
    assert "differs" in capsys.readouterr().out


def test_the_real_hook_names_a_slug_kb_publish_can_read():
    if not REAL_HOOK.exists():          # exported tree: nothing private to check
        assert not (ROOT / "publish.deny.toml").exists()
        return
    from kb_core.publish import hooks
    assert hooks.allowed_slug(ROOT)
