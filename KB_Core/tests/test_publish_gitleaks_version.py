"""gitleaks from PATH must be the pinned version, or say loudly that it isn't.

CI downloads the pinned, sha256-checked release; a local verify used whatever
`gitleaks` was on PATH (say, a newer release than the pinned one), whose newer rules
flagged content CI passes — "one definition, no drift" held for the script but
not for the scanner. The script now compares `gitleaks version` with
GITLEAKS_VERSION and warns on a mismatch; KB_GITLEAKS_STRICT_VERSION=1 makes it
a refusal (exit 2).
"""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "gitleaks.sh"
PINNED = re.search(r"^GITLEAKS_VERSION=(\S+)$", SCRIPT.read_text(), re.MULTILINE)[1]


def _run(tmp_path, version, **env):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "gitleaks"
    fake.write_text(f'#!/bin/sh\n[ "$1" = version ] && {{ echo "{version}"; exit 0; }}\n'
                    f'echo "scan $*" >> "{tmp_path}/scan.log"\nexit 0\n')
    fake.chmod(0o755)
    target = tmp_path / "tree"
    target.mkdir(exist_ok=True)
    return subprocess.run(["bash", str(SCRIPT), str(target)], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", **env})


def test_pinned_version_runs_quietly(tmp_path):
    r = _run(tmp_path, PINNED)
    assert r.returncode == 0 and "WARNING" not in r.stderr
    assert (tmp_path / "scan.log").exists()


def test_other_version_warns_and_names_both(tmp_path):
    r = _run(tmp_path, "8.99.0")
    assert r.returncode == 0
    assert "WARNING" in r.stderr and "8.99.0" in r.stderr and PINNED in r.stderr


def test_strict_mode_refuses_a_mismatch(tmp_path):
    r = _run(tmp_path, "v8.99.0", KB_GITLEAKS_STRICT_VERSION="1")
    assert r.returncode == 2 and "REFUSED" in r.stderr
    assert not (tmp_path / "scan.log").exists()


def test_v_prefix_is_the_same_version(tmp_path):
    assert "WARNING" not in _run(tmp_path, f"v{PINNED}").stderr
