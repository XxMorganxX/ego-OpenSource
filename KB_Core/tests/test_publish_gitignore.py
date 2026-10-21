"""Tool output never becomes a tracked (and so exportable) file.

Playwright, pytest and coverage write reports that can embed page text, test
data and absolute paths. They are also hard-denied from every export
(manifest.hard_deny_reason), but an ignore keeps them out of `git add -A` and
out of any commit automation's reach in the first place.
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

IGNORED = [
    "test-results/x.json", "playwright-report/index.html",
    "KB_Frontend/test-results/run/trace.zip", "KB_Frontend/playwright-report/index.html",
    "KB_Frontend/coverage/index.html", "KB_Frontend/.nyc_output/out.json",
    ".venv/bin/python", "KB_Agent/.venv/bin/python", "KB_Retrieval/.venv/pyvenv.cfg",
    ".pytest_cache/v/cache/nodeids", "KB_Agent/.pytest_cache/README.md",
    "KB_Core/.coverage", "KB_Core/.coverage.host.123", "KB_Core/coverage.xml",
    "KB_Core/htmlcov/index.html", "evals/retrieval/htmlcov/index.html",
]


def _ignored(rel: str) -> bool:
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=ROOT)
    assert r.returncode in (0, 1), f"git check-ignore failed for {rel}"
    return r.returncode == 0


@pytest.mark.parametrize("rel", IGNORED)
def test_tool_output_is_ignored(rel):
    assert _ignored(rel), rel


@pytest.mark.parametrize("rel", ["KB_Frontend/src/App.tsx", "KB_Core/src/kb_core/cli.py",
                                 "scripts/ci/python.sh", "evals/extraction/gold.jsonl"])
def test_sources_are_not_ignored(rel):
    assert not _ignored(rel), rel
