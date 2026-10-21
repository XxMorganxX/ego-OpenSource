"""CI decides, the tool obeys.

Required checks on the private repo cannot be assumed, so kb-publish itself asks
GitHub for the check-runs on the exact sha it is about to publish and refuses
unless every name in REQUIRED_CHECKS completed with success. The `gh api`
call is a seam: these tests feed it canned pages, never the network.
"""
import json
import re
from pathlib import Path

import pytest

from kb_core.publish import ci_attest
from kb_core.publish.ci_attest import CheckRun

SHA = "a" * 40
REPO = "acme/private-repo"


def _green(*names):
    return [CheckRun(n, "completed", "success", "github-actions",
                     2 if n in ci_attest.PUBLISH_CANDIDATE_CHECKS else 1) for n in names]


def _wf(repo, sha):
    # Provenance has its own tests (test_publish_ci_provenance.py); here both
    # workflows ran on a push to main.
    return [ci_attest.WorkflowRun(1, ci_attest.CI_WORKFLOW, "push", "main", sha),
            ci_attest.WorkflowRun(2, ci_attest.CANDIDATE_WORKFLOW, "push", "main", sha)]


def _lookup(runs):
    calls = []

    def fake(repo, sha):
        calls.append((repo, sha))
        return list(runs)
    fake.calls = calls
    return fake


def test_all_required_green_passes():
    lookup = _lookup(_green(*ci_attest.REQUIRED_CHECKS))
    assert ci_attest.attest(REPO, SHA, lookup, runs=_wf) == list(ci_attest.REQUIRED_CHECKS)
    assert lookup.calls == [(REPO, SHA)]


def test_extra_unrelated_runs_are_ignored():
    runs = _green(*ci_attest.REQUIRED_CHECKS) + [
        CheckRun("some other job", "completed", "failure", "github-actions")]
    ci_attest.attest(REPO, SHA, _lookup(runs), runs=_wf)


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r[1:], "missing"),
    (lambda r: [r[0]._replace(status="in_progress", conclusion=None), *r[1:]], "in_progress"),
    (lambda r: [r[0]._replace(conclusion="failure"), *r[1:]], "failure"),
    (lambda r: [r[0]._replace(conclusion="skipped"), *r[1:]], "skipped"),
    (lambda r: [r[0]._replace(conclusion="neutral"), *r[1:]], "neutral"),
    # a same-named run from another app is not CI's verdict
    (lambda r: [r[0]._replace(app="some-bot"), *r[1:]], "some-bot"),
    # duplicates: every run carrying a required name must be green
    (lambda r: [*r, r[0]._replace(conclusion="failure")], "failure"),
])
def test_anything_but_all_green_refuses(mutate, needle):
    runs = mutate(_green(*ci_attest.REQUIRED_CHECKS))
    with pytest.raises(ci_attest.CIAttestError) as e:
        ci_attest.attest(REPO, SHA, _lookup(runs), runs=_wf)
    msg = str(e.value)
    assert needle in msg and ci_attest.REQUIRED_CHECKS[0] in msg


def test_lookup_failure_refuses():
    def boom(repo, sha):
        raise ci_attest.CIAttestError("gh api failed: HTTP 404")
    with pytest.raises(ci_attest.CIAttestError):
        ci_attest.attest(REPO, SHA, boom, runs=_wf)


@pytest.mark.parametrize("sha", ["", "abc1234", "g" * 40, SHA.upper()[:39]])
def test_only_a_full_sha_is_attested(sha):
    with pytest.raises(ci_attest.CIAttestError):
        ci_attest.attest(REPO, sha, _lookup(_green(*ci_attest.REQUIRED_CHECKS)), runs=_wf)


# --- the default gh seam (fed canned output; nothing runs) ---------------------

class _Run:
    def __init__(self, stdout="", rc=0, stderr=""):
        self.stdout, self.rc, self.stderr, self.argv = stdout, rc, stderr, None

    def __call__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        return type("R", (), {"returncode": self.rc, "stdout": self.stdout,
                              "stderr": self.stderr})()


def _page(total, runs):
    return json.dumps({"total": total, "runs": [
        {"name": n, "status": s, "conclusion": c, "app": a} for n, s, c, a in runs]})


def test_gh_check_runs_paginates_and_parses():
    p1 = _page(3, [("a", "completed", "success", "github-actions"),
                   ("b", "completed", "failure", "github-actions")])
    p2 = _page(3, [("c", "queued", None, "github-actions")])
    run = _Run(p1 + "\n" + p2 + "\n")
    runs = ci_attest.gh_check_runs(REPO, SHA, run=run)
    assert [r.name for r in runs] == ["a", "b", "c"]
    assert runs[2] == CheckRun("c", "queued", None, "github-actions")
    assert run.argv[:3] == ["gh", "api", "--paginate"]
    assert any(f"repos/{REPO}/commits/{SHA}/check-runs" in a for a in run.argv)
    assert "GET" in run.argv or "-X" not in run.argv          # read-only
    assert run.kw["env"]["GH_HOST"] == "github.com"


@pytest.mark.parametrize("stdout,rc", [
    ("", 1),                                                    # gh failed
    ("not json\n", 0),
    (_page(5, [("a", "completed", "success", "github-actions")]), 0),   # incomplete
    ("", 0),                                                    # no pages at all
])
def test_gh_check_runs_fails_closed(stdout, rc):
    with pytest.raises(ci_attest.CIAttestError):
        ci_attest.gh_check_runs(REPO, SHA, run=_Run(stdout, rc, "HTTP 502"))


def test_gh_check_runs_refuses_bad_repo():
    with pytest.raises(ci_attest.CIAttestError):
        ci_attest.gh_check_runs("../evil", SHA, run=_Run())


# --- REQUIRED_CHECKS stays in step with the workflows ---------------------------

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _job_names(text: str) -> list[str]:
    """The `name:` of every job — what GitHub reports as the check-run name."""
    jobs = text.split("\njobs:\n", 1)[1]
    return re.findall(r"^  [A-Za-z0-9_-]+:\s*\n    name:\s*(.+?)\s*$", jobs, re.MULTILINE)


def test_every_ci_job_is_required():
    names = _job_names((_WORKFLOWS / "ci.yml").read_text())
    assert names, "no job names parsed from ci.yml"
    missing = [n for n in names if n not in ci_attest.REQUIRED_CHECKS]
    assert missing == [], f"ci.yml jobs not in REQUIRED_CHECKS: {missing}"


def test_publish_candidate_names_are_required():
    """The workflow is private-only: present exactly where the denylist is
    (tests/test_publish_ci_scripts.py pins that), and then its job names must
    be the ones the push gate asks GitHub for."""
    assert set(ci_attest.PUBLISH_CANDIDATE_CHECKS) <= set(ci_attest.REQUIRED_CHECKS)
    wf = _WORKFLOWS / "publish-candidate.yml"
    if (_WORKFLOWS.parents[1] / "publish.deny.toml").exists():
        assert sorted(_job_names(wf.read_text())) == sorted(ci_attest.PUBLISH_CANDIDATE_CHECKS)
