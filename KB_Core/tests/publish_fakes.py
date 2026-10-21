"""Test doubles for kb-publish's network seams — nothing here touches the network.

FakeGitHub stands in for the `github` seam (`gh api repos/<owner>/<name>`).
seams()/gated() satisfy kb-publish's pull-side push gate (CI check-runs,
gitleaks, verify, origin/main) without the network; tests that exercise one
gate override just that seam.
fake_ssh_command() builds a GIT_SSH_COMMAND that serves ssh://git@github.com/
<owner>/<name>.git (and the scp form) from local bare repos under a root dir —
the same env var a real deploy-key push uses, so the code under test runs its
real ssh code path against a local stand-in.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from kb_core.publish import ci_attest, github

PRIVATE = "acme/private-repo"
PUBLIC = "acme/mirror"
PRIVATE_URL = f"git@github.com:{PRIVATE}.git"
PUBLIC_URL = f"ssh://git@github.com/{PUBLIC}.git"
PRIVATE_ID = 1001
PUBLIC_ID = 2002


class FakeGitHub:
    """Callable seam: "owner/name" -> RepoInfo, lookups case-insensitive like GitHub."""

    def __init__(self, *repos: github.RepoInfo):
        self.repos = {r.full_name.lower(): r for r in repos}
        self.calls: list[str] = []

    def __call__(self, full_name: str) -> github.RepoInfo:
        self.calls.append(full_name)
        try:
            return self.repos[full_name.lower()]
        except KeyError:
            raise github.GitHubError(f"HTTP 404 Not Found (repos/{full_name})") from None

    def set(self, full_name: str, **changes) -> None:
        key = full_name.lower()
        self.repos[key] = self.repos[key]._replace(**changes)


def default_github(*, public_empty: bool = True) -> FakeGitHub:
    return FakeGitHub(
        github.RepoInfo(id=PRIVATE_ID, full_name=PRIVATE, visibility="private", empty=False),
        github.RepoInfo(id=PUBLIC_ID, full_name=PUBLIC, visibility="public",
                        empty=public_empty),
    )


_FAKE_SSH = '''\
import os, shlex, sys
root = sys.argv[1]
verb, path = shlex.split(sys.argv[-1])
if verb not in ("git-upload-pack", "git-receive-pack"):
    sys.exit("fake ssh: unexpected command " + sys.argv[-1])
os.execvp(verb, [verb, os.path.join(root, path.lstrip("/"))])
'''


def fake_ssh_command(tmp_path: Path, root: Path) -> str:
    script = tmp_path / "fake_ssh.py"
    script.write_text(_FAKE_SSH)
    return " ".join(shlex.quote(p) for p in (sys.executable, str(script), str(root)))


def bare(root: Path, full_name: str = PUBLIC) -> Path:
    """A bare repo at <root>/<owner>/<name>.git, default branch main."""
    path = root / f"{full_name}.git"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(path)],
                   check=True)
    return path


def wire_public(tmp_path: Path, root: Path, monkeypatch, *, pin: bool = True):
    """Give a scratch private repo an `origin` and a pinned public mirror.

    The mirror is a local bare repo served as PUBLIC_URL through a fake
    GIT_SSH_COMMAND; returns (bare_path, fake_github). The pin goes through the
    real `kb-publish pin`, so the state it writes is the state a push reads.
    """
    from kb_core.publish import cli

    subprocess.run(["git", "remote", "add", "origin", PRIVATE_URL], cwd=root, check=True)
    gh_root = tmp_path / "gh"
    remote = bare(gh_root)
    bare(gh_root, PRIVATE)          # origin itself, for the real `git fetch origin`
    monkeypatch.setenv("GIT_SSH_COMMAND", fake_ssh_command(tmp_path, gh_root))
    gh = default_github()
    if pin:
        assert cli.pin(root, repo=PUBLIC, github=gh) == 0
    return remote, gh


_SUITES = {ci_attest.CI_WORKFLOW: 501, ci_attest.CANDIDATE_WORKFLOW: 502}


def green_checks(repo: str, sha: str) -> list[ci_attest.CheckRun]:
    """check_runs seam: every required check-run completed with success, each in
    the check suite of the workflow that must produce it (see workflow_runs)."""
    return [ci_attest.CheckRun(n, "completed", "success", ci_attest.CI_APP,
                               _SUITES[ci_attest.WORKFLOW_OF[n]])
            for n in ci_attest.REQUIRED_CHECKS]


def workflow_runs(repo: str, sha: str) -> list[ci_attest.WorkflowRun]:
    """workflow_runs seam: ci.yml and publish-candidate.yml ran on a push to main."""
    return [ci_attest.WorkflowRun(suite, path, "push", "main", sha)
            for path, suite in _SUITES.items()]


def fake_fetch(root: Path) -> None:
    """fetch_origin seam: pretend origin/main is exactly this checkout's HEAD."""
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=root,
                   check=True)


def seams(**over):
    """cli.Seams with every gate green: CI passes, gitleaks "installed", verify a
    no-op, origin/main = HEAD, the running gate code accepted (scratch repos carry
    no kb_core), a scratch repo without a pre-push hook source accepted (a hook
    source that IS committed is still checked in full). Reading public HEAD stays
    real (local bare repos)."""
    from kb_core.publish import cli, hooks

    base = dict(fetch_origin=fake_fetch, check_runs=green_checks, workflow_runs=workflow_runs,
                which=lambda name: f"/fake/bin/{name}", verify=lambda tree, **kw: None,
                gate_code=lambda root, rev: None,
                hook_problem=lambda root, rev, private: hooks.problem(
                    root, rev=rev, private_full_name=private, missing_ok=True))
    base.update(over)
    return cli.Seams(**base)


def head(root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def ack(root: Path, remote: str = PUBLIC_URL, ref: str = "HEAD") -> str | None:
    """The --ack-gate-changes token a dry run would print for `ref`, or None when
    nothing in the gate changed — what an operator who READ the diff passes.

    Public HEAD is read through the fake ssh only; without one, from state alone
    (exactly what a dry run without a readable remote does)."""
    import os

    from kb_core.publish import cli, state

    sha = subprocess.run(["git", "rev-parse", ref], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    read = remote if "GIT_SSH_COMMAND" in os.environ else None
    try:
        review = cli.gate_review(root, sha, read_remote=read, st=state.load(root),
                                 seams=seams(), push=False)
    except cli.gate.GateError:
        return None             # the run itself refuses the same inconsistency
    return review.token


def gated(root: Path, **over) -> dict:
    """cli.run kwargs for a push that passes the pull-side gate at HEAD:
    --expect-sha names HEAD, --ack-gate-changes is the token for what changed (if
    anything), verify runs (as a no-op)."""
    sha = head(root)
    return dict(expect_sha=sha, ack_gate_changes=ack(root), skip_verify=False,
                seams=seams(**over))


def ensure_origin(root: Path) -> None:
    """A github.com origin (never contacted: pair with fake_fetch), pinned as the
    private repo the way `kb-publish pin` records it."""
    from kb_core.publish import state

    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, capture_output=True)
    if r.returncode != 0:
        subprocess.run(["git", "remote", "add", "origin", PRIVATE_URL], cwd=root, check=True)
    st = state.load(root)
    if st["private_repo_id"] is None:
        st.update(private_repo_id=PRIVATE_ID, private_repo_full_name=PRIVATE)
        state.save(root, st)


def push_origin(root: Path, ref: str = "HEAD") -> None:
    """Push `ref` to the private origin's main (through the fake ssh)."""
    subprocess.run(["git", "push", "-q", "-f", "origin", f"{ref}:refs/heads/main"], cwd=root,
                   check=True, capture_output=True)


MIRROR = ("ego-OS mirror", "mirror@invalid")
SMALL_MANIFEST = """
[publish]
remote = "/nonexistent/placeholder.git"
[export]
include = ["src/**", "publish.manifest.toml"]
"""


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(root: Path, msg: str = "c", *, approve: bool = True, **files: str | bytes) -> str:
    """Write files ('a__b' = 'a/b'), commit, approve + commit the lockfile; HEAD sha."""
    from kb_core.publish import cli, lock

    for rel, data in files.items():
        p = root / rel.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(data)
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg)
    if approve and cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0 and git(
            root, "status", "--porcelain", lock.LOCK_REL):
        git(root, "add", lock.LOCK_REL)
        git(root, "commit", "-qm", "approve")
    return head(root)


def scratch_private(tmp_path: Path, monkeypatch, *, deny: tuple[str, ...] = ("secretname",),
                    files: dict | None = None):
    """A private repo (manifest + deny + src/a.py, approved) with origin and a pinned
    public mirror wired; origin/main = HEAD. Returns (root, public_bare, fake_github).
    The publish identity is the mirror identity."""
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_NAME", MIRROR[0])
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_EMAIL", MIRROR[1])
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    patterns = ", ".join(f'"{p}"' for p in deny)
    commit(root, "init", **{"publish.manifest.toml": SMALL_MANIFEST,
                            "publish.deny.toml": f"[deny]\npatterns = [{patterns}]\n",
                            "src/a.py": "a = 1\n", **(files or {})})
    remote, gh = wire_public(tmp_path, root, monkeypatch)
    push_origin(root)
    return root, remote, gh


def gated_push(root: Path, gh, **over) -> int:
    """cli.run --push of HEAD through every real gate but CI/gitleaks/verify (seams).

    Acknowledges whatever the gate lists, exactly as an operator who read it would."""
    from kb_core.publish import cli

    sha = head(root)
    kw = dict(ref="HEAD", push=True, tag=None, skip_verify=False, remote=PUBLIC_URL,
              github=gh, expect_sha=sha, ack_gate_changes=ack(root),
              seams=seams(fetch_origin=_real_fetch))
    kw.update(over)
    return cli.run(root, **kw)


def _real_fetch(root: Path) -> None:
    from kb_core.publish import gate
    gate.fetch_origin(root)


def public_main(remote: Path) -> str | None:
    r = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/heads/main"],
                       cwd=remote, capture_output=True, text=True)
    return r.stdout.strip() or None
