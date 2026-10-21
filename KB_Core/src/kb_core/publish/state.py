"""Publish-state: lives under private/ (never exported) of the PRIMARY checkout.

One file per repository, not per checkout: publishing from a fresh `git
worktree add` checkout is the normal flow, and a per-worktree file lost the pin
(push refused), then the record of the push itself (last_published_* never
armed the ancestry check; imported PR credit stayed pending elsewhere) and
blocked `git worktree remove`. Every checkout resolves the path through the
shared git dir to <primary checkout>/private/publish-state.json — the same
place import_pr._main_root puts its worktrees. The file is gitignored: a
committed copy rolled the pin back on a branch switch.

Keys:
  last_published_ref         private sha of the last successful --push
  pending_coauthors          credited on the next sync commit (kb-import-pr):
                             {name, email, prs: [PR numbers]}; after the push,
                             kb-publish prints a `gh pr close` line per PR
  public_repo_id             GitHub's numeric id of THE public mirror — written only by
  public_repo_full_name        `kb-publish pin`; --push refuses without it
  last_published_public_sha  the sync commit that push created; the next push refuses
                             if public history no longer contains it
  private_repo_id            GitHub's numeric id of the PRIVATE repo (origin), recorded
  private_repo_full_name       by the same `kb-publish pin`; --push reads CI only from it
                             and refuses when origin names anything else

Files written before a key existed still load: missing keys take their default.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

_DEFAULTS = {
    "last_published_ref": None,
    "pending_coauthors": [],
    "public_repo_id": None,
    "public_repo_full_name": None,
    "last_published_public_sha": None,
    "private_repo_id": None,
    "private_repo_full_name": None,
}


_REL = Path("private") / "publish-state.json"


def path(root: Path) -> Path:
    """The state file for the repository `root` is a checkout of."""
    # No inherited GIT_DIR/GIT_COMMON_DIR: they could point at another repo.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       cwd=root, capture_output=True, text=True, env=env)
    if r.returncode == 0:
        common = Path(r.stdout.strip())
        if common.name == ".git":
            return common.parent / _REL
    return root / _REL          # not a checkout (or a bare/separate git dir)


def load(root: Path) -> dict:
    p = path(root)
    data = json.loads(p.read_text()) if p.exists() else {}
    # deepcopy: callers mutate pending_coauthors; the defaults must stay fresh.
    return {**copy.deepcopy(_DEFAULTS), **data}


def save(root: Path, data: dict) -> None:
    p = path(root)
    p.parent.mkdir(exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({**copy.deepcopy(_DEFAULTS), **data}, indent=2) + "\n")
    tmp.replace(p)
