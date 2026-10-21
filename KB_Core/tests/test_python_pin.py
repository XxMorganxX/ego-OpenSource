"""Every Python package runs on 3.13, locally (uv reads .python-version) and in CI.

The suites are verified on 3.13; without a pin, `uv run` can rebuild a live venv on an
older interpreter. requires-python may stay >=3.11; the pin is what uv picks."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGES = ("KB_Core", "KB_MCP", "KB_Retrieval", "KB_Agent")


def _job(ci: str, name: str) -> str:
    """One top-level job's text: from `  <name>:` to the blank line before the next job."""
    return ci.split(f"\n  {name}:\n", 1)[1].split("\n\n  ", 1)[0]


def test_each_package_pins_python_3_13():
    for pkg in PACKAGES:
        pin = ROOT / pkg / ".python-version"
        assert pin.is_file(), pkg
        assert pin.read_text(encoding="utf-8").strip() == "3.13", pkg


def test_ci_python_jobs_use_3_13():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for job in ("python", "kb-inference", "evals-tests"):
        assert 'UV_PYTHON: "3.13"' in _job(ci, job), job
