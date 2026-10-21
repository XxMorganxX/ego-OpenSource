"""Which repository a push may write: the pinned public mirror, and nothing else.

A remote URL alone is not a safe push target: if it ever names the PRIVATE
repo (a typo, a stale variable, a reused name), a push fast-forwards private
main to a `sync:` commit with private/ deleted. So the target is identified
by the numeric id GitHub assigned the public repo, recorded once by
`kb-publish pin` after checking it is public and not the private origin.
Every push re-checks, before anything is committed:

- the remote URL parses to a github.com owner/name (no ssh host aliases, no
  local paths: neither has an identity GitHub can vouch for);
- that owner/name is not the private origin's;
- GitHub answers for that exact name, without a rename redirect (a redirected
  old name can be claimed by someone else later, who would then receive the push);
- the repo is public, its id equals the pin, and its id is not origin's.

The private repo is pinned the same way (check_private): CI attestation
and the origin/main check used to trust whatever `origin` pointed at, so
swapping origin to a fork whose Actions someone else controls spoofed every
required check. --push now reads both only from the pinned private repo.

Every failure raises TargetError; callers print REFUSED and stop.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from kb_core.publish import github as github_api


class TargetError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    full_name: str
    repo_id: int
    empty: bool     # GitHub reports no branches: only then may a failed clone mean "empty"


def origin_full_name(root: Path) -> str:
    """owner/name of the private repo this checkout develops in (its `origin`)."""
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise TargetError("this checkout has no `origin` remote — cannot tell the private "
                          "repo apart from the push target")
    full = github_api.parse_remote(r.stdout.strip())
    if full is None:
        raise TargetError(f"`origin` ({r.stdout.strip()}) is not a github.com remote — "
                          f"cannot tell the private repo apart from the push target")
    return full


def _lookup(github: github_api.Lookup, full_name: str, what: str) -> github_api.RepoInfo:
    try:
        return github(full_name)
    except github_api.GitHubError as e:
        raise TargetError(f"GitHub lookup of {what} {full_name} failed: {e}") from e


def check_public(full_name: str, *, origin_full_name: str | None,
                 github: github_api.Lookup) -> github_api.RepoInfo:
    """The checks shared by `pin` and every push; returns GitHub's view of the repo."""
    if not github_api.valid_full_name(full_name):
        raise TargetError(f"{full_name!r} is not an owner/name")
    if origin_full_name is None:
        raise TargetError("the private origin is unknown — refusing to guess the target "
                          "is a different repo")
    if full_name.casefold() == origin_full_name.casefold():
        raise TargetError(f"{full_name} is the private origin — never a publish target")
    info = _lookup(github, full_name, "target")
    if info.full_name.casefold() != full_name.casefold():
        raise TargetError(f"{full_name} is a redirect to {info.full_name} (renamed repo) — "
                          f"use the canonical name; an old name can be re-registered by "
                          f"anyone")
    if info.visibility != "public":
        raise TargetError(f"{info.full_name} is not public (visibility: {info.visibility})")
    origin = _lookup(github, origin_full_name, "private origin")
    if origin.id == info.id:
        raise TargetError(f"{info.full_name} has the private origin's repo id {info.id}")
    return info


def validate(remote: str, *, pinned_id: int | None, pinned_full_name: str | None,
             origin_full_name: str | None, github: github_api.Lookup) -> Target:
    full = github_api.parse_remote(remote)
    if full is None:
        raise TargetError(f"remote {remote!r} is not a github.com owner/name URL "
                          f"(ssh://git@github.com/<owner>/<name>.git)")
    if pinned_id is None:
        raise TargetError("no public repo is pinned — run `kb-publish pin --repo "
                          "<owner>/<name>` once, after checking it is the new public repo")
    info = check_public(full, origin_full_name=origin_full_name, github=github)
    if info.id != pinned_id:
        raise TargetError(f"{info.full_name} has repo id {info.id}, but the pinned public "
                          f"repo is {pinned_id} ({pinned_full_name}) — a different repo "
                          f"lives at this name")
    if pinned_full_name and info.full_name.casefold() != pinned_full_name.casefold():
        raise TargetError(f"the pinned repo {pinned_id} was {pinned_full_name} and is now "
                          f"{info.full_name} — re-run `kb-publish pin --repo "
                          f"{info.full_name}` to acknowledge the rename")
    return Target(full_name=info.full_name, repo_id=info.id, empty=info.empty)


def check_origin_private(origin_full_name: str, *,
                         github: github_api.Lookup) -> github_api.RepoInfo:
    """GitHub's view of origin, which `pin` records as THE private repo."""
    info = _lookup(github, origin_full_name, "private origin")
    if info.full_name.casefold() != origin_full_name.casefold():
        raise TargetError(f"origin {origin_full_name} is a redirect to {info.full_name} "
                          f"(renamed repo) — point origin at the canonical name first")
    if info.visibility == "public":
        raise TargetError(f"origin {info.full_name} is not private (visibility: "
                          f"{info.visibility}) — it cannot be the private repo")
    return info


def check_private_name(origin_full_name: str, *, pinned_id: int | None,
                       pinned_full_name: str | None) -> str:
    """The pinned private repo's name, if origin still names it (no network).

    Runs before anything else in a push: CI attestation and origin/main are
    read from the name this returns, never from what origin says.
    """
    if pinned_id is None or not pinned_full_name:
        raise TargetError("no private repo is pinned — run `kb-publish pin --repo "
                          "<public owner>/<name>` again; it records origin as the private "
                          "repo whose CI a push obeys")
    if origin_full_name.casefold() != pinned_full_name.casefold():
        raise TargetError(f"origin names {origin_full_name}, not the pinned private repo "
                          f"{pinned_full_name} — CI and origin/main are read only from the "
                          f"pinned repo; restore origin (or re-pin with --replace)")
    return pinned_full_name


def check_private(origin_full_name: str, *, pinned_id: int | None,
                  pinned_full_name: str | None,
                  github: github_api.Lookup) -> github_api.RepoInfo:
    """origin must still be the pinned private repo, by name AND GitHub id.

    Checked with the public target, before anything is committed: a renamed
    private repo whose old name someone re-registered answers to the same URL.
    """
    check_private_name(origin_full_name, pinned_id=pinned_id,
                       pinned_full_name=pinned_full_name)
    info = _lookup(github, pinned_full_name, "pinned private repo")
    if info.full_name.casefold() != pinned_full_name.casefold():
        raise TargetError(f"the pinned private repo {pinned_full_name} now redirects to "
                          f"{info.full_name} — re-pin deliberately with --replace")
    if info.id != pinned_id:
        raise TargetError(f"{pinned_full_name} now has repo id {info.id}, but the pinned "
                          f"private repo is {pinned_id} — a different repo lives at this name")
    if info.visibility == "public":
        raise TargetError(f"the pinned private repo {info.full_name} is public")
    return info
