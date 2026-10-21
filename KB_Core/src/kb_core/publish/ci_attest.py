"""CI decides, the tool obeys: the check-runs a sha needs before it may go public.

Server-side branch protection on the private repo cannot be assumed (not
every GitHub plan offers it, and kb-publish cannot see whether it is on), so
nothing guarantees a red or never-tested commit stays off main. The gate
therefore lives on the pull side: `kb-publish --push` asks GitHub for the
check-runs on the exact sha it is about to publish and refuses unless every
name in REQUIRED_CHECKS completed with `success`, reported by GitHub Actions.

A check-run's name is the JOB's `name:` field (not "workflow / job"). Keep
REQUIRED_CHECKS in step with the workflows: tests/test_publish_ci_attest.py
fails when a ci.yml job is missing here, and when publish-candidate.yml's job
names differ from PUBLISH_CANDIDATE_CHECKS.

Provenance: a check-run's name and app are labels any workflow can
produce — a side-branch workflow, or one that POSTs a check-run on main's sha
with a `checks: write` token. Each required check-run's check suite must
belong to a workflow run of the expected file (WORKFLOW_OF), triggered by push
or workflow_dispatch on main, for this exact sha; an API-created check-run has
no workflow run behind it and is refused.

The `gh api` calls are seams (`Lookup`, `RunsLookup`) so tests never reach the
network.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import NamedTuple

from kb_core.publish import github as github_api

# .github/workflows/ci.yml — exported, runs on both repos. Exactly its job
# names, in both directions (tests/test_publish_ci_scripts.py): a job missing
# here could be red at publish time; a name no job produces blocks every push.
CI_CHECKS: tuple[str, ...] = (
    "ruff (lint)",
    "pytest — KB_Core + KB_Retrieval + KB_MCP + KB_Agent",
    "pytest — KB_Inference",
    "pytest — evals",
    "build (tsc) + vitest — KB_Frontend",
    "secret scan (gitleaks)",
)
# .github/workflows/publish-candidate.yml — private only. Its two jobs MUST use
# exactly these `name:` values, or every push is refused as "missing".
PUBLISH_CANDIDATE_CHECKS: tuple[str, ...] = (
    "publish-candidate guard",
    "publish-candidate export-ci",
)
REQUIRED_CHECKS: tuple[str, ...] = CI_CHECKS + PUBLISH_CANDIDATE_CHECKS

# Only GitHub Actions' verdict counts: another installed app could post a
# same-named check-run.
CI_APP = "github-actions"

_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class CIAttestError(RuntimeError):
    """The sha is not provably green — callers refuse."""


class CheckRun(NamedTuple):
    name: str
    status: str             # queued / in_progress / completed / ...
    conclusion: str | None  # success / failure / skipped / neutral / ... (None until completed)
    app: str | None         # the reporting GitHub App's slug
    suite: int | None = None  # check_suite.id — joins the run to the workflow run behind it


class WorkflowRun(NamedTuple):
    suite: int              # check_suite_id
    path: str               # .github/workflows/<file>
    event: str              # push / workflow_dispatch / pull_request / ...
    head_branch: str | None
    head_sha: str


Lookup = Callable[[str, str], list[CheckRun]]
RunsLookup = Callable[[str, str], list[WorkflowRun]]

# Which workflow file must have produced each required check-run.
CI_WORKFLOW = ".github/workflows/ci.yml"
CANDIDATE_WORKFLOW = ".github/workflows/publish-candidate.yml"
WORKFLOW_OF: dict[str, str] = {**dict.fromkeys(CI_CHECKS, CI_WORKFLOW),
                               **dict.fromkeys(PUBLISH_CANDIDATE_CHECKS, CANDIDATE_WORKFLOW)}
_EVENTS = frozenset({"push", "workflow_dispatch"})


def _provenance(name: str, r: CheckRun, sha: str, by_suite: dict[int, WorkflowRun]) -> str | None:
    want = WORKFLOW_OF.get(name)
    if want is None:
        return f"{name}: no workflow is expected to produce it"
    w = by_suite.get(r.suite) if r.suite is not None else None
    if w is None:
        return (f"{name}: no workflow run behind check suite {r.suite} (an API-created "
                f"check-run?) — only {want} on main counts")
    if w.path != want:
        return f"{name}: produced by {w.path}, not {want}"
    if w.head_sha != sha or w.head_branch != "main" or w.event not in _EVENTS:
        return (f"{name}: from a {w.event} run on {w.head_branch}@{w.head_sha[:12]}, not a "
                f"push to main at {sha[:12]}")
    return None


def attest(repo: str, sha: str, lookup: Lookup,
           required: tuple[str, ...] = REQUIRED_CHECKS, *, runs: RunsLookup) -> list[str]:
    """Raise CIAttestError unless every required check-run on `sha` is green.

    Fail-closed: a missing name, a pending run, any conclusion but success
    (skipped and neutral included), another app's run, ANY of several
    same-named runs not green, or a run not produced by the expected workflow
    on a push to main at `sha` (`runs`) is a refusal. Returns the names
    it verified.
    """
    if not _SHA.match(sha):
        raise CIAttestError(f"{sha!r} is not a full commit sha — CI attests exact commits")
    workflow_runs = runs(repo, sha)
    by_suite = {w.suite: w for w in workflow_runs}
    runs = lookup(repo, sha)
    problems = []
    for name in required:
        mine = [r for r in runs if r.name == name]
        if not mine:
            problems.append(f"{name}: missing (never ran on {sha[:12]})")
            continue
        for r in mine:
            if r.app != CI_APP:
                problems.append(f"{name}: reported by {r.app!r}, not {CI_APP}")
            elif r.status != "completed":
                problems.append(f"{name}: {r.status}")
            elif r.conclusion != "success":
                problems.append(f"{name}: {r.conclusion}")
            elif why := _provenance(name, r, sha, by_suite):
                problems.append(why)
    if problems:
        raise CIAttestError(f"{repo}@{sha[:12]} is not green on every required check:\n  "
                            + "\n  ".join(problems))
    return list(required)


_GH_TIMEOUT_S = 120
_JQ = ('{total: .total_count, runs: [.check_runs[] | '
       '{name, status, conclusion, app: .app.slug, suite: .check_suite.id}]}')
_RUNS_JQ = ('{total: .total_count, runs: [.workflow_runs[] | '
            '{suite: .check_suite_id, path, event, head_branch, head_sha}]}')


def gh_check_runs(repo: str, sha: str, *, run=subprocess.run) -> list[CheckRun]:
    """Default Lookup: `gh api --paginate .../commits/<sha>/check-runs` (GET only).

    One JSON document per page; the sum of runs must equal GitHub's
    total_count, so a truncated pagination is a refusal, not a pass.
    """
    if not github_api.valid_full_name(repo):
        raise CIAttestError(f"not an owner/name: {repo!r}")
    path = f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest"
    try:
        r = run(["gh", "api", "--paginate", "-X", "GET", path, "--jq", _JQ],
                capture_output=True, text=True, env=github_api._gh_env(),
                timeout=_GH_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CIAttestError(f"gh api {path} timed out after {_GH_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise CIAttestError(f"gh api could not run ({exc}) — is gh installed?") from exc
    if r.returncode != 0:
        raise CIAttestError(f"gh api {path} failed: {(r.stderr or '').strip()[-200:]}")
    runs: list[CheckRun] = []
    total = None
    pages = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not pages:
        raise CIAttestError(f"gh api {path} returned nothing")
    for line in pages:
        try:
            page = json.loads(line)
            total = page["total"] if total is None else total
            runs.extend(CheckRun(x["name"], x["status"], x.get("conclusion"), x.get("app"),
                                 x.get("suite"))
                        for x in page["runs"])
        except (ValueError, KeyError, TypeError) as exc:
            raise CIAttestError(f"gh api {path}: unexpected output") from exc
    if total != len(runs):
        raise CIAttestError(f"gh api {path}: got {len(runs)} check-runs of {total} — "
                            f"incomplete pagination")
    return runs


def gh_workflow_runs(repo: str, sha: str, *, run=subprocess.run) -> list[WorkflowRun]:
    """Default RunsLookup: `gh api --paginate .../actions/runs?head_sha=<sha>` (GET only)."""
    if not github_api.valid_full_name(repo):
        raise CIAttestError(f"not an owner/name: {repo!r}")
    path = f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100"
    try:
        r = run(["gh", "api", "--paginate", "-X", "GET", path, "--jq", _RUNS_JQ],
                capture_output=True, text=True, env=github_api._gh_env(),
                timeout=_GH_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CIAttestError(f"gh api {path} timed out after {_GH_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise CIAttestError(f"gh api could not run ({exc}) — is gh installed?") from exc
    if r.returncode != 0:
        raise CIAttestError(f"gh api {path} failed: {(r.stderr or '').strip()[-200:]}")
    out: list[WorkflowRun] = []
    total = None
    pages = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not pages:
        raise CIAttestError(f"gh api {path} returned nothing")
    for line in pages:
        try:
            page = json.loads(line)
            total = page["total"] if total is None else total
            out.extend(WorkflowRun(int(x["suite"]), str(x["path"]), str(x["event"]),
                                   x.get("head_branch"), str(x["head_sha"]))
                       for x in page["runs"])
        except (ValueError, KeyError, TypeError) as exc:
            raise CIAttestError(f"gh api {path}: unexpected output") from exc
    if total != len(out):
        raise CIAttestError(f"gh api {path}: got {len(out)} workflow runs of {total} — "
                            f"incomplete pagination")
    return out
