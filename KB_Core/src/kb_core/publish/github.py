"""The GitHub seam: which repository a remote URL names, and what GitHub says it is.

kb-publish must know WHICH repository it is about to write before it writes
anything: pointed at the private repo, a push would fast-forward private
main to a `sync:` commit that deletes private/. A URL alone proves
nothing — names are reused, renamed and redirected — so the push gate turns
the URL into "owner/name", asks GitHub for that repo's numeric id and
visibility, and compares the id with the one pinned in private state.

Everything that talks to GitHub goes through a `Lookup` callable so tests
inject a fake; the default shells out to `gh api` (a read-only GET).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from typing import NamedTuple


class GitHubError(RuntimeError):
    """A lookup that could not produce a trustworthy answer — callers refuse."""


class RepoInfo(NamedTuple):
    id: int
    full_name: str      # as GitHub reports it (canonical case, post-rename)
    visibility: str     # "public" only when GitHub says so unambiguously
    empty: bool         # no branches at all — the only state where a failed clone is tolerable


Lookup = Callable[[str], RepoInfo]

# GitHub's own rules: owners are alphanumerics and single hyphens (not leading);
# repo names are [A-Za-z0-9._-]. "." and ".." are not repos and would turn
# "repos/<owner>/<name>" into a different API path.
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_NAME = r"[A-Za-z0-9._-]{1,100}"
_FULL = re.compile(rf"^(?P<owner>{_OWNER})/(?P<name>{_NAME})$")

# Only github.com itself. An ssh host alias (git@github-deploy:...) may resolve
# anywhere via ~/.ssh/config, so it is not a GitHub identity — use
# ssh://git@github.com/... with GIT_SSH_COMMAND selecting the deploy key instead.
_REMOTE = re.compile(
    r"^(?:git@github\.com:"
    r"|ssh://git@(?:github\.com(?::22)?|ssh\.github\.com:443)/"
    r"|https://(?:[^@/\s]+@)?github\.com/)"
    rf"(?P<owner>{_OWNER})/(?P<name>{_NAME}?)(?:\.git)?/?$"
)


def valid_full_name(full_name: str) -> bool:
    m = _FULL.match(full_name)
    return bool(m) and m["name"] not in (".", "..", ".git")


def parse_remote(url: str) -> str | None:
    """"owner/name" for a github.com remote URL, else None (caller refuses)."""
    m = _REMOTE.match(url)
    if not m:
        return None
    full = f"{m['owner']}/{m['name']}"
    return full if valid_full_name(full) else None


_GH_TIMEOUT_S = 60


def _gh_env() -> dict:
    # GH_HOST from the shell could point `gh api` at an enterprise host whose
    # "acme/mirror" is a different repository; GH_REPO is irrelevant to an
    # explicit path but is dropped so nothing implicit leaks in.
    env = {k: v for k, v in os.environ.items() if k not in ("GH_REPO",)}
    env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    return env


def _gh_json(path: str, run) -> object:
    try:
        r = run(["gh", "api", path], capture_output=True, text=True,
                env=_gh_env(), timeout=_GH_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"gh api {path} timed out after {_GH_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise GitHubError(f"gh api {path} could not run ({exc}) — is gh installed?") from exc
    if r.returncode != 0:
        raise GitHubError(f"gh api {path} failed: {(r.stderr or '').strip()[-200:]}")
    try:
        return json.loads(r.stdout)
    except ValueError as exc:
        raise GitHubError(f"gh api {path} returned non-JSON output") from exc


def gh_repo(full_name: str, *, run=subprocess.run) -> RepoInfo:
    """Default Lookup: `gh api repos/<owner>/<name>` (GET only)."""
    if not valid_full_name(full_name):
        raise GitHubError(f"not an owner/name: {full_name!r}")
    data = _gh_json(f"repos/{full_name}", run)
    if not isinstance(data, dict):
        raise GitHubError(f"repos/{full_name}: unexpected response shape")
    repo_id, name = data.get("id"), data.get("full_name")
    if type(repo_id) is not int or not isinstance(name, str):
        raise GitHubError(f"repos/{full_name}: response lacks an integer id / full_name")
    # Public only when nothing says otherwise: `private: true` wins over any
    # visibility string, and a missing field is read from `private` alone.
    if data.get("private") is not False:
        visibility = data.get("visibility") or "private"
        visibility = "private" if visibility == "public" else visibility
    else:
        visibility = data.get("visibility") or "public"
    empty = False
    if data.get("size") == 0:
        # size is in KB and rounds tiny repos down to 0; "no branches" is the
        # real emptiness signal. If that second call fails, empty stays False —
        # it is the permissive flag, so unknown means not empty.
        try:
            empty = _gh_json(f"repos/{full_name}/branches?per_page=1", run) == []
        except GitHubError:
            empty = False
    return RepoInfo(id=repo_id, full_name=name, visibility=visibility, empty=empty)
