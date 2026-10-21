"""Hard-deny near-misses: the names private material actually gets.

The hard-deny was literal: `.env.local` was denied but a bare `.env` was not,
and `private_notes/`, `scratchpad/`, `temp/`, `secrets/`, `notes-PRIVATE.md`,
`.envrc`/`.npmrc`/`.netrc`, ssh keys and parallel coverage files were all
selectable by the real manifest under KB_Core/** and evals/** — held back only
by a lockfile approval that can be given blind. Still a pure function of the
path: no TOML can turn it off.
"""
from pathlib import Path

import pytest

from kb_core.publish import manifest

ROOT = Path(__file__).resolve().parents[2]
REAL = manifest.load_manifest(ROOT / "publish.manifest.toml")

NEAR_MISSES = [
    "KB_Core/.env", "KB_Core/.env.production", "KB_Core/.envrc", "KB_Core/.npmrc",
    "KB_Core/.netrc", "KB_Core/.pypirc", "KB_Core/id_ed25519", "KB_Core/id_rsa.pub",
    "KB_Core/certs/server.pem", "KB_Core/client.p12",
    "KB_Core/private_notes/x.md", "KB_Core/scratchpad/x.md", "KB_Core/temp/x.md",
    "KB_Core/secrets/token.txt", "evals/retrieval/notes-PRIVATE.md",
    "KB_Core/.coverage.host.1234.567890", "KB_Core/my-scratch-dir/a.py",
    # A whole token, so a module named for it is held back too: export-ci goes
    # red on the missing import (fail closed) rather than it shipping unread.
    "KB_Core/src/kb_core/private_fields.py",
]


@pytest.mark.parametrize("rel", NEAR_MISSES)
def test_near_miss_is_hard_denied(rel):
    assert manifest.hard_deny_reason(rel), rel
    assert manifest.select([rel], REAL) == []


@pytest.mark.parametrize("rel", [
    "KB_Core/.env.example", "KB_MCP/.env.tunnel.example", "KB_Core/src/kb_core/environment.py",
    "KB_Core/src/kb_core/templates/x.py", "KB_Core/src/kb_core/identity.py",
    "KB_Core/tests/test_publish_origin_pin.py", "KB_Core/src/kb_core/temperature.py",
])
def test_ordinary_names_are_not(rel):
    assert manifest.hard_deny_reason(rel) is None, rel


def test_the_real_export_loses_nothing_it_selected_before():
    # Every currently selected path still selects: the rules are near-misses only.
    import subprocess
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                           check=True).stdout.decode().split("\0")
    selected = manifest.select([f for f in files if f], REAL)
    assert all(manifest.hard_deny_reason(p) is None for p in selected)
    assert len(selected) > 400
