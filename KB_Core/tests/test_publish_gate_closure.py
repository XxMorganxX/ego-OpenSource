"""Everything that executes inside the gate process is a gate file.

kb-publish and the strict CI guard import `kb_core` before any gate code runs,
so `kb_core/__init__.py` executes inside the gate: three appended lines there
can replace gate.gate_changes and publish a weakened denylist with no
--ack-gate-changes, and a public PR carrying them must not import without
--allow-control-files. The same holds for what decides which code is
installed (KB_Core/pyproject.toml, KB_Core/uv.lock) and for what Python runs
at interpreter start (sitecustomize/usercustomize, *.pth). kb_core.publish
imports nothing else from kb_core (asserted below), so this list is the whole
closure.
"""
import ast
from pathlib import Path

import pytest

from kb_core.publish import gate, import_pr

PUBLISH_DIR = Path(gate.__file__).parent


@pytest.mark.parametrize("rel", [
    "KB_Core/src/kb_core/__init__.py", "kb_core/src/KB_CORE/__init__.py",
    "KB_Core/pyproject.toml", "KB_Core/uv.lock",
    "sitecustomize.py", "KB_Core/src/sitecustomize.py", "x/usercustomize.py",
    "KB_Core/src/evil.pth", "Lib/site-packages/x.PTH",
])
def test_code_that_runs_inside_the_gate_is_a_gate_file(rel):
    assert gate.is_gate_file(rel), rel


@pytest.mark.parametrize("rel", [
    "KB_Core/src/kb_core/__init__.py", "KB_Core/pyproject.toml", "KB_Core/uv.lock",
])
def test_and_an_import_control_file(rel):
    assert import_pr.control_reason(rel) == "publish gate file"


def test_ordinary_package_modules_are_not_gate_files():
    for rel in ("KB_Core/src/kb_core/cli.py", "KB_Core/src/kb_core/ingest/__init__.py",
                "KB_Retrieval/pyproject.toml", "KB_Core/tests/conftest.py"):
        assert not gate.is_gate_file(rel), rel


def test_kb_core_publish_imports_nothing_else_from_kb_core():
    # If a publish module ever imports kb_core.<other>, that module runs inside
    # the gate too and must join gate._GATE_EXACT — this test is the reminder.
    for f in sorted(PUBLISH_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            else:
                continue
            for mod in mods:
                # bare `kb_core` is its __init__.py — already a gate file
                if mod.startswith("kb_core."):
                    assert mod.startswith("kb_core.publish"), f"{f.name} imports {mod}"


def test_gate_changes_lists_kb_core_init(tmp_path):
    import subprocess

    def git(*a):
        return subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True,
                              text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    init = tmp_path / "KB_Core/src/kb_core/__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text('"""pkg"""\n')
    git("add", "-A")
    git("commit", "-qm", "a")
    base = git("rev-parse", "HEAD")
    init.write_text('"""pkg"""\nfrom kb_core.publish import gate as _g\n'
                    "_g.gate_changes = lambda *a, **k: []\n")
    git("commit", "-qam", "b")
    assert gate.gate_changes(tmp_path, base, git("rev-parse", "HEAD")) == [
        ("M", "KB_Core/src/kb_core/__init__.py")]
