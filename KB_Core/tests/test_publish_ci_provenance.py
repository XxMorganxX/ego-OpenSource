"""A required check-run counts only if the right workflow produced it on main.

attest pinned the app slug (github-actions) and nothing else: a check-run with
the right NAME from any workflow — a side-branch workflow, or one that POSTs a
check-run on main's sha with a `checks: write` token — counted as green. Each
required check-run's check suite must now belong to a workflow run of the
expected file (ci.yml for the CI jobs, publish-candidate.yml for the guard and
export-ci), triggered by push (or workflow_dispatch) on main, for this exact
sha. An API-created check-run has no workflow run behind it and is refused.
"""
import json

import pytest

from kb_core.publish import ci_attest
from kb_core.publish.ci_attest import CheckRun, WorkflowRun

SHA = "b" * 40
REPO = "acme/private-repo"
CI, CAND = 11, 22


def _checks(**over):
    runs = []
    for n in ci_attest.REQUIRED_CHECKS:
        suite = CAND if n in ci_attest.PUBLISH_CANDIDATE_CHECKS else CI
        runs.append(CheckRun(n, "completed", "success", "github-actions", over.get(n, suite)))
    return lambda repo, sha: runs


def _wf(*runs):
    default = [WorkflowRun(CI, ".github/workflows/ci.yml", "push", "main", SHA),
               WorkflowRun(CAND, ".github/workflows/publish-candidate.yml", "push", "main",
                           SHA)]
    return lambda repo, sha: list(runs) if runs else default


def test_runs_from_the_right_workflows_pass():
    assert ci_attest.attest(REPO, SHA, _checks(), runs=_wf())


def test_api_created_check_run_without_a_workflow_run_refused():
    with pytest.raises(ci_attest.CIAttestError, match="no workflow run"):
        ci_attest.attest(REPO, SHA, _checks(**{"publish-candidate guard": 999}), runs=_wf())


@pytest.mark.parametrize("bad", [
    WorkflowRun(CAND, ".github/workflows/sneaky.yml", "push", "main", SHA),
    WorkflowRun(CAND, ".github/workflows/publish-candidate.yml", "push", "wt-x", SHA),
    WorkflowRun(CAND, ".github/workflows/publish-candidate.yml", "pull_request", "main", SHA),
    WorkflowRun(CAND, ".github/workflows/publish-candidate.yml", "push", "main", "c" * 40),
    WorkflowRun(CAND, ".github/workflows/ci.yml", "push", "main", SHA),   # wrong file
])
def test_wrong_workflow_branch_event_or_sha_refused(bad):
    good_ci = WorkflowRun(CI, ".github/workflows/ci.yml", "push", "main", SHA)
    with pytest.raises(ci_attest.CIAttestError):
        ci_attest.attest(REPO, SHA, _checks(), runs=_wf(good_ci, bad))


def test_workflow_dispatch_on_main_is_accepted():
    runs = _wf(WorkflowRun(CI, ".github/workflows/ci.yml", "push", "main", SHA),
               WorkflowRun(CAND, ".github/workflows/publish-candidate.yml",
                           "workflow_dispatch", "main", SHA))
    assert ci_attest.attest(REPO, SHA, _checks(), runs=runs)


class _Run:
    def __init__(self, stdout="", rc=0):
        self.stdout, self.rc, self.argv = stdout, rc, None

    def __call__(self, argv, **kw):
        self.argv = argv
        return type("R", (), {"returncode": self.rc, "stdout": self.stdout, "stderr": ""})()


def test_gh_workflow_runs_paginates_parses_and_is_read_only():
    page = json.dumps({"total": 2, "runs": [
        {"suite": CI, "path": ".github/workflows/ci.yml", "event": "push",
         "head_branch": "main", "head_sha": SHA},
        {"suite": CAND, "path": ".github/workflows/publish-candidate.yml", "event": "push",
         "head_branch": "main", "head_sha": SHA}]})
    run = _Run(page + "\n")
    got = ci_attest.gh_workflow_runs(REPO, SHA, run=run)
    assert [w.suite for w in got] == [CI, CAND]
    assert run.argv[:3] == ["gh", "api", "--paginate"] and "GET" in run.argv
    assert any(f"repos/{REPO}/actions/runs?head_sha={SHA}" in a for a in run.argv)


@pytest.mark.parametrize("stdout,rc", [("", 1), ("nope\n", 0),
                                       (json.dumps({"total": 3, "runs": []}), 0)])
def test_gh_workflow_runs_fails_closed(stdout, rc):
    with pytest.raises(ci_attest.CIAttestError):
        ci_attest.gh_workflow_runs(REPO, SHA, run=_Run(stdout, rc))


def test_check_run_suite_is_parsed():
    page = json.dumps({"total": 1, "runs": [{"name": "a", "status": "completed",
                                             "conclusion": "success", "app": "github-actions",
                                             "suite": 5}]})
    assert ci_attest.gh_check_runs(REPO, SHA, run=_Run(page))[0].suite == 5
