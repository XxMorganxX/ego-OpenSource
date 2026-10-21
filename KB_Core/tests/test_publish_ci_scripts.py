"""One definition of "the suites pass": scripts/ci/<job>.sh.

ci.yml's jobs, publish-candidate's export-ci job and kb-publish's local verify
all run the same scripts, so they cannot drift apart again (verify had silently
lost KB_Agent, the second tenancy database and the eval tests). These tests read the
workflow files as text — the exported tree has no YAML dependency to lean on.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

from kb_core.publish import ci_attest, manifest, verify

ROOT = Path(__file__).resolve().parents[2]
CI_DIR = ROOT / "scripts" / "ci"
WORKFLOWS = ROOT / ".github" / "workflows"
CI_YML = WORKFLOWS / "ci.yml"
CANDIDATE = WORKFLOWS / "publish-candidate.yml"

# job id in ci.yml == script stem == verify step label
JOBS = ["gitleaks", "lint", "python", "kb-inference", "evals-tests", "frontend"]


def _jobs(text: str) -> dict[str, str]:
    """{job id: that job's block of text}, for the top-level `jobs:` map."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):[ \t]*\n", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _name(block: str) -> str:
    return re.search(r"^    name:\s*(.+?)\s*$", block, re.MULTILINE).group(1)


# --- the scripts ----------------------------------------------------------------

def test_one_script_per_job():
    assert sorted(p.name for p in CI_DIR.glob("*.sh")) == sorted(f"{j}.sh" for j in JOBS)


@pytest.mark.parametrize("job", JOBS)
def test_script_is_strict_bash_and_parses(job):
    path = CI_DIR / f"{job}.sh"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert os.access(path, os.X_OK), f"{path} is not executable"
    subprocess.run(["bash", "-n", str(path)], check=True)


def _fake_bin(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH whose uv/uvx/npm/python only record that they were called."""
    bindir, log = tmp_path / "bin", tmp_path / "calls.log"
    bindir.mkdir()
    for tool in ("uv", "uvx", "npm", "python", "python3", "gitleaks"):
        (bindir / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\nexit 0\n')
        (bindir / tool).chmod(0o755)
    return bindir, log


def _env(bindir: Path, **extra) -> dict:
    keep = {k: v for k, v in os.environ.items()
            if k in ("HOME", "LANG", "TMPDIR") or k.startswith("LC_")}
    return {**keep, "PATH": f"{bindir}:/usr/bin:/bin", **extra}


def test_python_script_refuses_without_a_test_database(tmp_path):
    """Fixtures TRUNCATE their database: never fall back to a default one."""
    bindir, log = _fake_bin(tmp_path)
    r = subprocess.run(["bash", str(CI_DIR / "python.sh")], capture_output=True, text=True,
                       env=_env(bindir), cwd=tmp_path)
    assert r.returncode != 0
    assert "KB_TEST_DATABASE_URL" in r.stderr
    assert not log.exists(), "ran tools before refusing"


def test_python_script_runs_every_package_against_a_and_b(tmp_path):
    bindir, log = _fake_bin(tmp_path)
    url = "postgresql://u:p@127.0.0.1:1/kb_verify_x?sslmode=disable"
    r = subprocess.run(["bash", str(CI_DIR / "python.sh")], capture_output=True, text=True,
                       env=_env(bindir, KB_TEST_DATABASE_URL=url), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    for pkg_marker in ("prepare_test_dbs.py", "--cov=kb_core", "validate.py"):
        assert pkg_marker in calls
    assert calls.count("uv sync") == 4                 # KB_Core, KB_Retrieval, KB_MCP, KB_Agent
    assert calls.count("uv run --locked pytest") == 4
    # A pyproject edited without re-locking must fail CI, not silently re-resolve.
    assert calls.count("uv sync --locked") == 4
    assert all("--locked" in c for c in calls.splitlines() if c.startswith("uv "))
    assert "golden.jsonl --static" in calls and "golden_package.jsonl --static" in calls
    assert "mcp_surface/golden.jsonl --static" in calls
    assert "KB_TEST_DATABASE_URL_B=postgresql://u:p@127.0.0.1:1/kb_verify_x_b?sslmode=disable" \
        in r.stdout


def test_evals_script_runs_each_tests_dir_on_its_own(tmp_path):
    bindir, log = _fake_bin(tmp_path)
    r = subprocess.run(["bash", str(CI_DIR / "evals-tests.sh")], capture_output=True,
                       text=True, env=_env(bindir), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    dirs = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "evals").glob("*/tests")
                  if p.is_dir())
    assert dirs, "the tree has eval tests; the script must run them"
    ran = [c for c in calls if "pytest" in c]
    assert len(ran) == len(dirs)
    for d in dirs:
        assert any(c.rstrip().endswith(d) or f" {d} " in c for c in ran), d


def test_gitleaks_script_pins_version_and_checksum():
    text = (CI_DIR / "gitleaks.sh").read_text(encoding="utf-8")
    assert re.search(r"^GITLEAKS_VERSION=\d+\.\d+\.\d+$", text, re.MULTILINE)
    assert re.search(r"^GITLEAKS_SHA256=[0-9a-f]{64}$", text, re.MULTILINE)
    assert "sha256sum -c" in text
    assert not re.search(r"curl[^\n]*\|\s*tar", text), "download must be verified before use"


# --- verify runs the same scripts -----------------------------------------------

def test_verify_steps_are_the_scripts_in_order():
    assert [label for label, _cwd, _argv in verify.STEPS] == JOBS
    for label, cwd, argv in verify.STEPS:
        assert cwd == "." and argv == ["bash", f"scripts/ci/{label}.sh"]


# --- ci.yml ---------------------------------------------------------------------

def test_ci_jobs_are_exactly_the_scripts():
    jobs = _jobs(CI_YML.read_text(encoding="utf-8"))
    assert sorted(jobs) == sorted(JOBS)
    for job, block in jobs.items():
        runs = re.findall(r"scripts/ci/([a-z0-9-]+)\.sh", block)
        assert runs == [job], f"ci.yml job {job} must run exactly scripts/ci/{job}.sh"


def test_ci_job_names_are_exactly_ci_checks():
    """Both directions: a ci.yml job the push gate ignores is a check that can
    be red at publish time; a CI_CHECKS name no job produces blocks every push."""
    jobs = _jobs(CI_YML.read_text(encoding="utf-8"))
    assert sorted(_name(b) for b in jobs.values()) == sorted(ci_attest.CI_CHECKS)


def test_publish_guard_is_gone_from_exported_ci():
    text = CI_YML.read_text(encoding="utf-8")
    assert "kb-check-publishable" not in text and "publish guard" not in text.lower()


def test_python_jobs_pin_3_13():
    jobs = _jobs(CI_YML.read_text(encoding="utf-8"))
    for job in ("python", "evals-tests", "kb-inference"):
        assert 'UV_PYTHON: "3.13"' in jobs[job], job


def test_ci_jobs_are_bounded_and_drop_the_checkout_token():
    text = CI_YML.read_text(encoding="utf-8")
    for job, block in _jobs(text).items():
        assert re.search(r"^    timeout-minutes: \d+$", block, re.MULTILINE), job
        assert re.search(r"^    runs-on: ubuntu-\d\d\.\d\d$", block, re.MULTILINE), job
        checkouts = block.count("actions/checkout@")
        assert block.count("persist-credentials: false") == checkouts, job


def test_ci_concurrency_never_cancels_main():
    """main's runs are what ci_attest reads at publish time: never cancel them."""
    head = CI_YML.read_text(encoding="utf-8").split("\njobs:\n", 1)[0]
    assert "\nconcurrency:\n" in head
    assert "cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}" in head


_USES =re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.MULTILINE)


@pytest.mark.parametrize("wf", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_every_action_is_pinned_by_full_sha(wf):
    text = (WORKFLOWS / wf).read_text(encoding="utf-8")
    uses = _USES.findall(text)
    assert uses
    for ref, rest in uses:
        assert re.fullmatch(r"[a-z0-9-]+/[a-z0-9._-]+@[0-9a-f]{40}", ref), ref
        assert re.fullmatch(r"\s*# v\d+\.\d+\.\d+\s*", rest), f"{ref}: needs '# vX.Y.Z'"


@pytest.mark.parametrize("wf", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_no_unverified_download_in_workflows(wf):
    text = (WORKFLOWS / wf).read_text(encoding="utf-8")
    assert not re.search(r"curl[^\n]*\|\s*(tar|sh|bash)", text)


@pytest.mark.parametrize("wf", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_workflows_are_read_only(wf):
    text = (WORKFLOWS / wf).read_text(encoding="utf-8")
    assert re.search(r"^permissions:\n  contents: read\n", text, re.MULTILINE), wf
    assert "write" not in re.findall(r"^permissions:\n((?:  .*\n)+)", text, re.MULTILINE)[0]


def test_scripts_are_exported():
    m = manifest.load_manifest(ROOT / "publish.manifest.toml")
    rels = [f"scripts/ci/{j}.sh" for j in JOBS] + ["scripts/ci/prepare_test_dbs.py"]
    assert sorted(manifest.select(rels, m)) == sorted(rels)


# --- publish-candidate.yml (private only) ----------------------------------------

def test_candidate_workflow_is_never_exported():
    rel = ".github/workflows/publish-candidate.yml"
    assert manifest.hard_deny_reason(rel)
    m = manifest.load_manifest(ROOT / "publish.manifest.toml")
    assert rel in m.exclude, "also excluded by name, so the intent is visible in the manifest"
    assert manifest.select([rel], m) == []


def test_candidate_workflow_exists_exactly_where_the_denylist_does():
    """Private repo (has publish.deny.toml): the workflow must be there, or the
    push gate's two required checks can never pass and nobody notices why.
    Exported tree (no denylist): it must not be there."""
    if (ROOT / "publish.deny.toml").exists():
        assert CANDIDATE.exists()
    else:
        assert not CANDIDATE.exists()


def _candidate() -> str:
    if not (ROOT / "publish.deny.toml").exists():
        pytest.skip("public tree: publish-candidate.yml is private-only")
    return CANDIDATE.read_text(encoding="utf-8")


def test_candidate_job_names_are_the_required_checks():
    jobs = _jobs(_candidate())
    assert sorted(_name(b) for b in jobs.values()) == sorted(ci_attest.PUBLISH_CANDIDATE_CHECKS)


def test_candidate_runs_only_on_main_and_by_hand():
    text = _candidate()
    on = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert "pull_request" not in on                    # secrets never meet PR code
    assert re.search(r"push:\n\s+branches:\s*\[\s*main\s*\]", on)
    assert "workflow_dispatch" in on


def test_candidate_guard_steps():
    guard = _jobs(_candidate())["guard"]
    order = ["kb-check-publishable --strict", "kb-publish export", "gitleaks.sh",
             "kb-publish diff", "upload-artifact"]
    positions = [guard.index(s) for s in order]
    assert positions == sorted(positions)
    assert "secrets.PUBLISH_DENY_CANARIES" in guard
    assert 'GITHUB_STEP_SUMMARY' in guard
    assert "fetch-depth: 0" in guard


def test_candidate_export_ci_runs_every_script_inside_the_export():
    job = _jobs(_candidate())["export-ci"]
    assert "needs: guard" in job
    assert "pgvector/pgvector:" in job
    assert "download-artifact" in job
    assert "scripts/ci/*.sh" in job
    # never the private checkout: no actions/checkout in this job
    assert "actions/checkout" not in job


def test_only_the_canary_secret_is_used():
    assert set(re.findall(r"secrets\.([A-Z_]+)", _candidate())) == {"PUBLISH_DENY_CANARIES"}
    for wf in WORKFLOWS.glob("*.yml"):
        if wf != CANDIDATE:
            assert "secrets." not in wf.read_text(encoding="utf-8"), wf.name
