"""kb-publish --push obeys CI and the public record.

Scratch private repo with a github.com `origin` and a pinned public mirror,
both local bare repos reached through a fake GIT_SSH_COMMAND, so `git fetch
origin` and the public-HEAD read run for real. GitHub's check-runs, gitleaks'
presence and verify are seams (tests/publish_fakes.seams).
"""
import os
import subprocess

import pytest

from kb_core.publish import ci_attest, cli, git_ops, lock, state, verify
from tests.publish_fakes import (PRIVATE, PUBLIC_URL, ack, green_checks, head, push_origin,
                                 seams, wire_public)

MANIFEST = """
[publish]
remote = "{remote}"
[export]
include = ["src/**", "publish.manifest.toml"]
"""
DENY = '[deny]\npatterns = ["secretname", "othername"]\n'


@pytest.fixture(autouse=True)
def _mirror_identity(monkeypatch):
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_NAME", "ego-OS mirror")
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_EMAIL", "mirror@invalid")


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(root, msg="c", approve=True, **files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)
    if approve and cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0 and _git(
            root, "status", "--porcelain", lock.LOCK_REL):
        _git(root, "add", lock.LOCK_REL)
        _git(root, "commit", "-qm", "approve")
    return head(root)


def _repo(tmp_path, monkeypatch, *, manifest_remote="/nonexistent/placeholder.git"):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _commit(root, "init", **{"publish.manifest.toml": MANIFEST.format(remote=manifest_remote),
                             "publish.deny.toml": DENY, "src/a.py": "a = 1\n"})
    remote, gh = wire_public(tmp_path, root, monkeypatch)
    push_origin(root)
    return root, remote, gh


def _push(root, gh, **kw):
    """A push through every real gate except CI/gitleaks/verify (seams)."""
    sha = head(root)
    args = dict(ref="HEAD", push=True, tag=None, skip_verify=False, remote=PUBLIC_URL,
                github=gh, expect_sha=sha, ack_gate_changes=ack(root),
                seams=seams(fetch_origin=cli.gate.fetch_origin))
    args.update(kw)
    return cli.run(root, **args)


def _main(remote):
    r = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/heads/main"],
                       cwd=remote, capture_output=True, text=True)
    return r.stdout.strip() or None


def test_gated_push_succeeds(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh) == 0
    out = capsys.readouterr().out
    assert "on origin/main" in out and "CI green" in out
    assert _main(remote)


# --- 1. --expect-sha, origin/main ----------------------------------------------

def test_skip_verify_with_push_refused_before_anything(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    calls = []
    s = seams(check_runs=lambda *a: calls.append(a) or green_checks(*a))
    assert _push(root, gh, skip_verify=True, seams=s) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "--skip-verify" in out
    assert calls == [] and _main(remote) is None


@pytest.mark.parametrize("expect", [None, "", "HEAD~1", "short"])
def test_push_needs_the_exact_expected_sha(tmp_path, monkeypatch, capsys, expect):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    _commit(root, "two", **{"src/a.py": "a = 2\n"})
    push_origin(root)
    if expect == "HEAD~1":
        expect = _git(root, "rev-parse", "HEAD~1")
    elif expect == "short":
        expect = head(root)[:12]
    assert _push(root, gh, expect_sha=expect) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "--expect-sha" in out
    assert _main(remote) is None


def test_dry_run_checks_expect_sha_when_given(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    rc = cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True,
                 expect_sha="0" * 40)
    assert rc == 1 and "--expect-sha" in capsys.readouterr().out


def test_unpushed_sha_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    _commit(root, "local only", **{"src/a.py": "a = 2\n"})        # not on origin
    assert _push(root, gh) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "origin/main" in out
    assert _main(remote) is None


def test_side_branch_sha_refused(tmp_path, monkeypatch, capsys):
    # A worktree/feature branch pushed to origin — but not on main — had no main CI.
    root, remote, gh = _repo(tmp_path, monkeypatch)
    _git(root, "checkout", "-q", "-b", "wt-x")
    _commit(root, "wip", **{"src/a.py": "a = 3\n"})
    _git(root, "push", "-q", "origin", "wt-x")
    assert _push(root, gh) == 1
    assert "origin/main" in capsys.readouterr().out
    assert _main(remote) is None


def test_fetch_failure_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)

    def boom(root):
        raise cli.gate.GateError("git fetch origin failed: network down")
    assert _push(root, gh, seams=seams(fetch_origin=boom)) == 1
    assert "REFUSED" in capsys.readouterr().out and _main(remote) is None


def test_no_origin_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    _git(root, "remote", "remove", "origin")
    assert _push(root, gh) == 1
    assert "origin" in capsys.readouterr().out and _main(remote) is None


# --- 2. CI attestation ------------------------------------------------------------

def test_ci_red_refused_and_names_the_check(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    seen = []

    def red(repo, sha):
        seen.append((repo, sha))
        runs = green_checks(repo, sha)
        return [runs[0]._replace(conclusion="failure"), *runs[1:]]
    assert _push(root, gh, seams=seams(fetch_origin=cli.gate.fetch_origin,
                                       check_runs=red)) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and ci_attest.REQUIRED_CHECKS[0] in out
    assert seen == [(PRIVATE, head(root))]       # slug from origin, the exact sha
    assert _main(remote) is None


def test_ci_lookup_error_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)

    def err(repo, sha):
        raise ci_attest.CIAttestError("gh api failed: HTTP 401")
    assert _push(root, gh, seams=seams(check_runs=err)) == 1
    assert "REFUSED" in capsys.readouterr().out and _main(remote) is None


# --- 7. gitleaks, verify in a copy -------------------------------------------------

def test_gitleaks_missing_refuses_push(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh, seams=seams(which=lambda name: None)) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "gitleaks" in out and _main(remote) is None


def test_dry_run_tolerates_missing_gitleaks(tmp_path, monkeypatch):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    got = {}
    s = seams(which=lambda name: None, verify=lambda tree, **kw: got.update(kw))
    assert cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=False, seams=s) == 0
    assert got["require_gitleaks"] is False


def test_push_requires_gitleaks_inside_verify(tmp_path, monkeypatch):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    got = {}
    assert _push(root, gh, seams=seams(verify=lambda tree, **kw: got.update(kw))) == 0
    assert got["require_gitleaks"] is True


def test_verify_runs_in_a_copy(tmp_path, monkeypatch):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    seen = {}

    def noisy(tree, **kw):
        seen["tree"] = tree
        (tree / "src" / "test-output.json").write_text('{"leak": 1}\n')     # a verify byproduct
        (tree / ".pytest_cache").mkdir()
        (tree / ".pytest_cache" / "x").write_text("y")
    assert _push(root, gh, seams=seams(verify=noisy)) == 0
    files = _git(remote, "ls-tree", "-r", "--name-only", "main").split()
    assert files == ["publish.manifest.toml", "src/a.py"]
    assert seen["tree"].name != "export"


def test_export_tree_changed_during_verify_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)

    def escape(tree, **kw):
        (tree.parent / "export" / "src" / "planted.py").write_text("x = 1\n")
    assert _push(root, gh, seams=seams(verify=escape)) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "changed during verify" in out
    assert _main(remote) is None


def test_verify_failure_refused_rc2(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)

    def boom(tree, **kw):
        raise verify.VerifyError("fake-step", "boom")
    assert _push(root, gh, seams=seams(verify=boom)) == 2
    assert "REFUSED" in capsys.readouterr().out and _main(remote) is None


# --- 3. no rollback ------------------------------------------------------------------

def test_rollback_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    one = head(root)
    two = _commit(root, "two", **{"src/a.py": "a = 2\n"})
    push_origin(root)
    assert _push(root, gh) == 0
    published = _main(remote)
    capsys.readouterr()
    # `one` is on origin/main and was green — but older than what public has.
    assert _push(root, gh, ref=one, expect_sha=one, ack_gate_changes=None) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "rollback" in out and two[:7] in out
    assert _main(remote) == published


def test_state_disagreeing_with_public_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh) == 0
    two = _commit(root, "two", **{"src/a.py": "a = 2\n"})
    push_origin(root)
    st = state.load(root)
    st["last_published_ref"] = two        # state claims a publish public never saw
    state.save(root, st)
    capsys.readouterr()
    assert _push(root, gh) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_second_publish_uses_public_head_as_base(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    one = head(root)
    assert _push(root, gh) == 0
    _commit(root, "two", **{"src/a.py": "a = 2\n"})
    push_origin(root)
    capsys.readouterr()
    assert _push(root, gh, ack_gate_changes=None) == 0          # no gate file changed
    out = capsys.readouterr().out
    assert f"upstream {one[:7]}" in out and "0 gate file" in out


# --- 4. gate-file changes ------------------------------------------------------------

def test_first_publish_needs_gate_ack(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh, ack_gate_changes=None) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "--ack-gate-changes" in out
    assert "publish.manifest.toml" in out and "publish.deny.toml" in out
    assert _main(remote) is None


def test_gate_change_needs_ack_of_this_exact_sha(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    one = head(root)
    assert _push(root, gh) == 0
    manifest = MANIFEST.format(remote="/nonexistent/placeholder.git").replace(
        '"src/**"', '"src/**", "extra/**"')
    two = _commit(root, "widen", **{"publish.manifest.toml": manifest})
    push_origin(root)
    capsys.readouterr()
    assert _push(root, gh, ack_gate_changes=None) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "M publish.manifest.toml" in out and two in out
    assert _push(root, gh, ack_gate_changes=one) == 1               # a sha is no ack
    assert _push(root, gh, ack_gate_changes=two) == 1               # ... not even this one
    assert _push(root, gh, ack_gate_changes=ack(root)) == 0         # the dry run's token


def test_removed_deny_pattern_needs_ack_and_is_not_printed(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert _push(root, gh) == 0
    two = _commit(root, "weaken", **{"publish.deny.toml": '[deny]\npatterns = ["secretname"]\n'})
    push_origin(root)
    capsys.readouterr()
    assert _push(root, gh, ack_gate_changes=None) == 1
    out = capsys.readouterr().out
    assert "1 deny pattern(s) removed" in out and "othername" not in out
    assert _push(root, gh, ack_gate_changes=two) == 1
    assert _push(root, gh, ack_gate_changes=ack(root)) == 0


def test_dry_run_prints_gate_changes_without_refusing(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    sha = head(root)
    assert cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True,
                   remote=PUBLIC_URL) == 0
    out = capsys.readouterr().out
    assert "publish.deny.toml" in out and f"--ack-gate-changes {ack(root)}" in out
    assert sha not in out.split("--ack-gate-changes", 1)[1]


def test_wrong_ack_refused_even_on_dry_run(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    assert cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True,
                   ack_gate_changes="1" * 40) == 1
    assert "REFUSED" in capsys.readouterr().out


# --- dry runs never contact the placeholder remote ------------------------------------

def test_dry_run_never_clones_the_manifest_placeholder(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch,
                             manifest_remote="https://github.com/example/ego-OS.git")
    monkeypatch.delenv("KB_PUBLISH_REMOTE", raising=False)

    def _no_network(*a, **kw):
        raise AssertionError("dry run contacted a remote")
    monkeypatch.setattr(git_ops, "_clone", _no_network)
    s = seams(public_head=_no_network, fetch_origin=_no_network, check_runs=_no_network)
    assert cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True, seams=s) == 0
    out = capsys.readouterr().out
    assert "not contacted" in out and "dry run ok" in out


def test_dry_run_with_explicit_remote_reads_public_head(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)
    one = head(root)
    assert _push(root, gh) == 0
    _commit(root, "two", **{"src/a.py": "a = 2\n"})
    capsys.readouterr()
    assert cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True,
                   remote=PUBLIC_URL) == 0
    assert f"upstream {one[:7]}" in capsys.readouterr().out


def test_push_cannot_read_public_head_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = _repo(tmp_path, monkeypatch)

    def unreadable(remote):
        raise cli.gate.GateError("reading public main failed: timeout")
    assert _push(root, gh, seams=seams(public_head=unreadable)) == 1
    assert "REFUSED" in capsys.readouterr().out and _main(remote) is None


def test_main_parses_the_new_flags(tmp_path, monkeypatch):
    got = {}
    monkeypatch.setattr(cli, "run", lambda root, **kw: got.update(kw) or 0)
    monkeypatch.setattr(cli, "_toplevel", lambda: tmp_path)
    assert cli.main(["--push", "--expect-sha", "a" * 40, "--ack-gate-changes", "a" * 40]) == 0
    assert got["expect_sha"] == "a" * 40 and got["ack_gate_changes"] == "a" * 40
    assert cli.main([]) == 0 and got["push"] is False          # bare kb-publish = dry run


def test_env_does_not_leak_into_tests():
    # Guard for the suite itself: nothing here may reach a real remote.
    assert "github.com/example" not in os.environ.get("KB_PUBLISH_REMOTE", "")
