"""The gate that judges a change is the one already public.

kb-publish imports kb_core, so code in the commit being published —
kb_core/__init__.py, kb_core/publish/** — runs before any check and can switch
the checks off (three appended lines can replace gate.gate_changes and ship a
weakened denylist with no ack). Listing those files as gate files only helps
if the code doing the listing is not the code under review. So --push now
refuses unless the gate code it is RUNNING is byte-identical to that code at
the last published upstream (at the sha itself for a first publish). A change
to the gate is therefore judged — and its diff shown for --ack-gate-changes —
by the previous, already-public gate; the next publish then runs the new one.
Uncommitted edits in an editable checkout, or a venv built from some other
commit, are refused the same way.
"""
import shutil
from pathlib import Path

import kb_core
from kb_core.publish import cli, gate
from tests.publish_fakes import (commit, gated_push, git, head, public_main, push_origin,
                                 scratch_private, seams)

PKG = Path(kb_core.__file__).parent
REL = "KB_Core/src/kb_core"


def _vendor_running_gate(root):
    """Commit a copy of the gate code this test process is running."""
    dest = root / REL
    (dest / "publish").mkdir(parents=True, exist_ok=True)
    shutil.copy(PKG / "__init__.py", dest / "__init__.py")
    for f in sorted((PKG / "publish").glob("*.py")):
        shutil.copy(f, dest / "publish" / f.name)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "vendor gate")


def test_running_code_matching_the_rev_passes(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    _vendor_running_gate(root)
    assert gate.running_gate_problem(root, head(root)) is None


def test_a_changed_init_is_named(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    _vendor_running_gate(root)
    (root / REL / "__init__.py").write_text("PAYLOAD = 1\n")
    git(root, "commit", "-qam", "payload")
    problem = gate.running_gate_problem(root, head(root))
    assert problem and "kb_core/__init__.py" in problem


def test_rev_without_the_package_is_a_problem(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    (root / "a").write_text("a\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "a")
    assert gate.running_gate_problem(root, head(root))


def test_push_runs_the_check_against_the_last_published_upstream(tmp_path, monkeypatch,
                                                                    capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    asked = []

    def gate_code(root_, rev):
        asked.append(rev)
        return None
    first = head(root)
    assert gated_push(root, gh, seams=seams(gate_code=gate_code,
                                            fetch_origin=gate.fetch_origin)) == 0
    assert asked == [first]                     # first publish: the sha itself
    commit(root, "two", **{"src/a.py": "a = 2\n"})
    push_origin(root)
    asked.clear()
    assert gated_push(root, gh, seams=seams(gate_code=gate_code,
                                            fetch_origin=gate.fetch_origin)) == 0
    assert asked == [first]                     # then: the last published upstream


def test_push_refused_when_the_running_gate_is_not_that_code(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    s = seams(gate_code=lambda root_, rev: f"kb_core/__init__.py differs from {rev[:7]}",
              fetch_origin=gate.fetch_origin)
    assert gated_push(root, gh, seams=s) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "kb_core/__init__.py" in out and "venv" in out
    assert public_main(remote) is None


def test_dry_run_only_notes_it(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    s = seams(gate_code=lambda root_, rev: "kb_core/__init__.py differs")
    capsys.readouterr()
    assert cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True, seams=s) == 0
    assert "NOTE" in capsys.readouterr().out
