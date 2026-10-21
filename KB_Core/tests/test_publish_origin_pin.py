"""The private repo is pinned like the public one.

CI attestation and the origin/main check used to trust whatever `origin`
pointed at: `git remote set-url origin git@github.com:evil/scratch.git` (a
fork whose Actions the attacker controls, or a careless second clone) turned
a red private commit into "CI green — 8 required check-runs" and a push.
`kb-publish pin` now records the private origin's GitHub id and canonical name
alongside the public pin; --push refuses unless origin still names that repo
and GitHub still resolves the name to that id, and check-runs are read from
the PINNED name, never from what origin says. kb-import-pr refuses a swapped
origin the same way.
"""
import subprocess

import pytest

from kb_core.publish import ci_attest, cli, github, import_pr, lock, state
from tests import import_pr_fakes as ipf
from tests.publish_fakes import (PRIVATE, PRIVATE_ID, PUBLIC, PUBLIC_URL, ack, bare,
                                 green_checks, head, push_origin, seams, wire_public)

MANIFEST = """
[publish]
remote = "/nonexistent/placeholder.git"
[export]
include = ["src/**", "publish.manifest.toml"]
"""
DENY = '[deny]\npatterns = ["secretname"]\n'
EVIL = "evil/scratch"


@pytest.fixture(autouse=True)
def _mirror_identity(monkeypatch):
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_NAME", "ego-OS mirror")
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_EMAIL", "mirror@invalid")


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    for rel, text in {"publish.manifest.toml": MANIFEST, "publish.deny.toml": DENY,
                      "src/a.py": "a = 1\n"}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    _git(root, "add", lock.LOCK_REL)
    _git(root, "commit", "-qm", "approve")
    remote, gh = wire_public(tmp_path, root, monkeypatch)
    push_origin(root)
    gh.repos[EVIL] = github.RepoInfo(id=6666, full_name=EVIL, visibility="public", empty=False)
    return root, remote, gh


def _checks(asked):
    def lookup(repo, sha):
        asked.append(repo)
        if repo == PRIVATE:
            return [ci_attest.CheckRun(n, "completed", "failure", ci_attest.CI_APP)
                    for n in ci_attest.REQUIRED_CHECKS]
        return green_checks(repo, sha)
    return lookup


def _push(root, gh, s):
    sha = head(root)
    return cli.run(root, ref="HEAD", push=True, tag=None, skip_verify=False,
                   remote=PUBLIC_URL, github=gh, expect_sha=sha, ack_gate_changes=ack(root),
                   seams=s)


def _main(remote):
    r = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/heads/main"],
                       cwd=remote, capture_output=True, text=True)
    return r.stdout.strip() or None


def test_pin_records_the_private_origin_too(tmp_path, monkeypatch):
    root, _, _ = _repo(tmp_path, monkeypatch)
    st = state.load(root)
    assert (st["private_repo_id"], st["private_repo_full_name"]) == (PRIVATE_ID, PRIVATE)


def test_red_private_ci_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    asked = []
    assert _push(root, gh, seams(check_runs=_checks(asked))) == 1
    assert asked == [PRIVATE] and _main(remote) is None


def test_origin_swap_cannot_spoof_ci(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    bare(tmp_path / "gh", EVIL)
    _git(root, "remote", "set-url", "origin", f"git@github.com:{EVIL}.git")
    _git(root, "push", "-q", "origin", "HEAD:refs/heads/main")
    asked = []
    assert _push(root, gh, seams(check_runs=_checks(asked),
                                 fetch_origin=cli.gate.fetch_origin)) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "pinned private repo" in out and EVIL in out
    assert EVIL not in asked and _main(remote) is None


def test_push_without_a_private_pin_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    st = state.load(root)
    st.update(private_repo_id=None, private_repo_full_name=None)   # a state file from before the private pin existed
    state.save(root, st)
    assert _push(root, gh, seams()) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "no private repo is pinned" in out
    assert _main(remote) is None
    # Re-running the same public pin records it (no --replace needed).
    assert cli.pin(root, repo=PUBLIC, github=gh) == 0
    assert state.load(root)["private_repo_id"] == PRIVATE_ID


def test_private_name_now_resolving_elsewhere_refused(tmp_path, monkeypatch, capsys):
    # The private repo was renamed and someone registered its old name: same
    # URL, different repository.
    root, remote, gh = _repo(tmp_path, monkeypatch)
    gh.set(PRIVATE, id=4242)
    assert _push(root, gh, seams()) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "4242" in out and _main(remote) is None


def test_private_repo_redirected_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    gh.repos[PRIVATE.lower()] = gh.repos[PRIVATE.lower()]._replace(
        full_name="acme/renamed-private")
    assert _push(root, gh, seams()) == 1
    assert "REFUSED" in capsys.readouterr().out and _main(remote) is None


def test_attest_reads_the_pinned_canonical_name(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    _git(root, "remote", "set-url", "origin", "git@github.com:ACME/Private-Repo.git")
    asked = []

    def lookup(repo, sha):
        asked.append(repo)
        return green_checks(repo, sha)
    assert _push(root, gh, seams(check_runs=lookup)) == 0
    assert asked == [PRIVATE]


def test_pin_refuses_to_silently_move_the_private_pin(tmp_path, monkeypatch, capsys):
    root, _, gh = _repo(tmp_path, monkeypatch)
    gh.repos["other/private"] = github.RepoInfo(id=3003, full_name="other/private",
                                                visibility="private", empty=False)
    _git(root, "remote", "set-url", "origin", "git@github.com:other/private.git")
    assert cli.pin(root, repo=PUBLIC, github=gh) == 1
    assert "private" in capsys.readouterr().out
    assert state.load(root)["private_repo_id"] == PRIVATE_ID
    assert cli.pin(root, repo=PUBLIC, github=gh, replace=True) == 0
    assert state.load(root)["private_repo_id"] == 3003


def test_pin_refuses_a_public_origin(tmp_path, monkeypatch, capsys):
    root, _, gh = _repo(tmp_path, monkeypatch)
    gh.set(PRIVATE, visibility="public")
    assert cli.pin(root, repo=PUBLIC, github=gh, replace=True) == 1
    assert "not private" in capsys.readouterr().out


# --- kb-import-pr --------------------------------------------------------------

def test_import_refuses_an_origin_that_is_not_the_pinned_private_repo(tmp_path, capsys):
    root = ipf.private_repo(tmp_path)
    st = state.load(root)
    st.update(private_repo_id=PRIVATE_ID, private_repo_full_name=PRIVATE)
    state.save(root, st)
    _git(root, "remote", "set-url", "origin", f"git@github.com:{EVIL}.git")
    patch = ipf.contributor_patch(root, ipf.write("src/a.py", "a = 2\n"))
    calls = []
    assert import_pr.run(root, 5, seams=ipf.seams(patch, calls=calls)) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "pinned private repo" in out
    assert calls == []
