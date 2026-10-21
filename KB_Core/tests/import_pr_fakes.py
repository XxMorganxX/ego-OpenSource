"""Scratch repos and real `git format-patch` output for kb-import-pr tests.

Patches are produced by git itself from a contributor clone, so the parser is
tested against exactly what `gh pr diff --patch` hands it (renames, copies,
mode changes, symlinks, binary patches) rather than hand-typed approximations.
Hand-written patches are used only for shapes git would never emit on its own
(path traversal, gitlinks, quoted names). Nothing here touches the network.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from kb_core.publish import import_pr, state
from tests.publish_fakes import PRIVATE_URL, PUBLIC, PUBLIC_ID

MANIFEST = """\
[publish]
remote = "https://github.com/example/ego-OS.git"

[export]
include = [
  "src/**", "docs/**", "scripts/**", "conftest.py", "pyproject.toml", "package.json",
  "uv.lock", "publish.manifest.toml", ".github/**", "KB_Core/**",
]
exclude = ["src/internal/**"]
"""
DENY = '[deny]\npatterns = ["zorblax"]\n'
PYPROJECT = """\
[project]
name = "demo"
version = "0.1.0"
description = "demo package"
dependencies = ["requests>=2"]

[project.scripts]
demo = "demo:main"

[tool.ruff]
line-length = 100
"""
PACKAGE_JSON = json.dumps({
    "name": "demo", "version": "0.1.0", "description": "demo app",
    "scripts": {"test": "vitest"}, "dependencies": {"react": "18.3.1"},
}, indent=2) + "\n"

AUTHOR = import_pr.PrAuthor(login="pat-contrib", name="Pat Contributor", user_id=4242)
AUTHOR_EMAIL = "4242+pat-contrib@users.noreply.github.com"


def git(root: Path, *args: str, input: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, input=input).stdout


def private_repo(tmp_path: Path, *, pin: bool = True, deny: bool = True) -> Path:
    """A private checkout with an origin (github.com URL) and origin/main = HEAD."""
    root = tmp_path / "private"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "owner@example.com")
    git(root, "config", "user.name", "Owner")
    files = {
        "publish.manifest.toml": MANIFEST,
        "src/a.py": "a = 1\n",
        "src/b.py": "b = 1\n",
        "src/internal/x.py": "x = 1\n",
        "docs/guide.md": "# Guide\n\nHello.\n",
        "pyproject.toml": PYPROJECT,
        "package.json": PACKAGE_JSON,
        "private/notes.md": "private notes\n",
        "KB_Core/src/kb_core/publish/check.py": "GATE = True\n",
    }
    if deny:
        files["publish.deny.toml"] = DENY
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    git(root, "remote", "add", "origin", PRIVATE_URL)
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    if pin:
        st = state.load(root)
        st.update(public_repo_id=PUBLIC_ID, public_repo_full_name=PUBLIC)
        state.save(root, st)
    return root


def contributor_patch(root: Path, *edits: Callable[[Path], None],
                      fmt: tuple[str, ...] = ("-M",), name: str = "Pat Contributor",
                      email: str = "pat@example.com", message: str = "contrib change") -> str:
    """`git format-patch --stdout` of one commit per edit, made in a clone of `root`."""
    clone = Path(tempfile.mkdtemp(dir=root.parent, prefix="contrib-")) / "c"
    subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True,
                   capture_output=True)
    git(clone, "config", "user.name", name)
    git(clone, "config", "user.email", email)
    base = git(clone, "rev-parse", "HEAD").strip()
    for i, edit in enumerate(edits):
        edit(clone)
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", f"{message} {i + 1}" if len(edits) > 1 else message)
    return git(clone, "format-patch", "--stdout", *fmt, f"{base}..HEAD")


def write(rel: str, text: str) -> Callable[[Path], None]:
    def _edit(clone: Path) -> None:
        p = clone / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return _edit


def seams(patch: str, *, author: import_pr.PrAuthor | Exception = AUTHOR, calls=None,
          **over) -> import_pr.Seams:
    """Seams that serve `patch` and `author` and fake the origin fetch (no network)."""
    calls = calls if calls is not None else []

    def fetch_patch(repo: str, n: int) -> str:
        calls.append(("patch", repo, n))
        return patch

    def pr_author(repo: str, n: int) -> import_pr.PrAuthor:
        calls.append(("author", repo, n))
        if isinstance(author, Exception):
            raise author
        return author

    def fetch_origin(root: Path) -> None:
        calls.append(("fetch", str(root)))

    kw = dict(fetch_patch=fetch_patch, pr_author=pr_author, fetch_origin=fetch_origin,
              confirm=lambda prompt: False)
    kw.update(over)
    return import_pr.Seams(**kw)
