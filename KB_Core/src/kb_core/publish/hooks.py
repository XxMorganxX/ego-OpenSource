"""Is the allowlist pre-push hook installed, effective and pointed at the pinned repo?

private/githooks/pre-push (it stays in the private repo) refuses any `git push`
except to the private repo. A hook is easy to defeat without noticing: it may
never have been installed, `core.hooksPath` (set after install, in any scope)
silently disables it, and its hard-coded ALLOWED slug lets pushes through to
whatever repo holds that name — after a rename, possibly a public repo that
took the old name over. kb-publish cannot make git run a hook, but
it can refuse to publish from a clone where the hook is not doing its job:

- the installed copy at $(git rev-parse --git-common-dir)/hooks/pre-push must
  be byte-identical to the tracked source (and executable);
- core.hooksPath must be unset, so git looks there at all;
- ALLOWED must be the pinned private repo, and `kb-publish pin` refuses a
  public target the hook would allow.

The source is read from the commit being published, not the working tree:
an uncommitted delete or edit of the file (or a sparse checkout without it)
must not switch the check off or change what "byte-identical" means. In the
private repo (it carries publish.deny.toml) a commit without the hook source
is refused; a checkout without a deny file has no hook to check. The hook stays
advisory — `--no-verify` skips it — so the public repo's own push rules (only
the publish deploy key may write) remain the control that actually holds.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

HOOK_REL = Path("private") / "githooks" / "pre-push"
_ALLOWED = re.compile(r'^ALLOWED="([^"\s]+)"\s*$', re.MULTILINE)


class HookError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")}
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, env=env)


def source(root: Path) -> Path | None:
    p = root / HOOK_REL
    return p if p.is_file() else None


def _at(root: Path, rev: str, rel: Path) -> bytes | None:
    """The blob at `rev:rel`, or None when the commit has no such file."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")}
    r = subprocess.run(["git", "cat-file", "blob", f"{rev}:{rel.as_posix()}"], cwd=root,
                       capture_output=True, env=env)
    return r.stdout if r.returncode == 0 else None


def source_bytes(root: Path, rev: str | None = None) -> bytes | None:
    """The tracked hook source: at commit `rev`, else the working tree."""
    if rev is not None:
        return _at(root, rev, HOOK_REL)
    src = source(root)
    return src.read_bytes() if src is not None else None


def _slug(data: bytes) -> str:
    m = _ALLOWED.search(data.decode("utf-8", errors="replace"))
    if m is None:
        raise HookError(f"{HOOK_REL} has no ALLOWED=\"owner/name\" line")
    return m[1].casefold()


def allowed_slug(root: Path, rev: str | None = None) -> str | None:
    """The owner/name the tracked hook allows (lower case), None without a hook."""
    data = source_bytes(root, rev)
    return None if data is None else _slug(data)


def problem(root: Path, *, private_full_name: str | None, rev: str | None = None,
            missing_ok: bool = False) -> str | None:
    """Why the hook is not doing its job in this clone, or None.

    `rev` (the commit being published) is where the hook source is read from.
    A private repo (publish.deny.toml in the checkout or at `rev`) whose `rev`
    has no hook source is refused, unless `missing_ok` (test repos that carry
    no hook at all)."""
    src = source_bytes(root, rev)
    if src is None:
        private = ((root / "publish.deny.toml").exists()
                   or (rev is not None and _at(root, rev, Path("publish.deny.toml")) is not None))
        if missing_ok or not private:
            return None
        where = f"commit {rev[:12]}" if rev else "the working tree"
        return (f"{HOOK_REL} is missing from {where}, but this is the private repo (it "
                f"carries publish.deny.toml): restore the pre-push hook source (a gate file) "
                f"and install it")
    try:
        allowed = _slug(src)
    except HookError as e:
        return str(e)
    if private_full_name and allowed != private_full_name.casefold():
        return (f"the pre-push hook allows {allowed}, not the pinned private repo "
                f"{private_full_name} — renamed repos? git push would reach the wrong repo; "
                f"fix ALLOWED in {HOOK_REL} (a gate change) and reinstall")
    hooks_path = _git(root, "config", "--get", "core.hooksPath").stdout.strip()
    if hooks_path:
        return (f"core.hooksPath is set ({hooks_path}), so git never runs the pre-push hook "
                f"— `git config --unset core.hooksPath` (in whichever scope sets it)")
    r = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if r.returncode != 0:
        return "cannot locate this clone's git dir to check the pre-push hook"
    installed = Path(r.stdout.strip()) / "hooks" / "pre-push"
    fix = f"run `sh {HOOK_REL.parent}/install.sh` once in this clone"
    if not installed.is_file():
        return f"the pre-push hook is not installed at {installed} — {fix}"
    if installed.read_bytes() != src:
        return f"the installed pre-push hook {installed} differs from {HOOK_REL} — {fix} " \
               f"(--force)"
    if not os.access(installed, os.X_OK):
        return f"the installed pre-push hook {installed} is not executable — {fix} (--force)"
    return None
