"""kb-import-pr's entry point refuses an untrusted patch source before any gh call.

The hardened checks (paths, control files, deny scan, worktree, credit) live
in test_publish_import_pr.py and test_publish_import_parse.py.
"""
import subprocess
import sys

import pytest

from kb_core.publish import import_pr

PLACEHOLDER_MANIFEST = (
    '[publish]\nremote = "https://github.com/example/ego-OS.git"\n'
    '[export]\ninclude = ["src/**", "publish.manifest.toml"]\n'
)


def test_main_refuses_manifest_placeholder_remote_before_any_gh_call(
        tmp_path, monkeypatch, capsys):
    # With KB_PUBLISH_REMOTE unset and no pinned public repo, the only remote
    # left is the manifest's fork-facing placeholder. Like kb-publish
    # (test_publish_cli.test_manifest_fallback_remote_refuses_push), it must
    # refuse before ever calling `gh` — never apply a patch fetched from a repo
    # nobody controls.
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "publish.manifest.toml").write_text(PLACEHOLDER_MANIFEST)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    monkeypatch.delenv("KB_PUBLISH_REMOTE", raising=False)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "argv", ["kb-import-pr", "7"])

    real_run = subprocess.run

    def _guarded_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "gh":
            pytest.fail(f"gh must not be invoked when the remote is the "
                        f"manifest's fork-facing placeholder: {cmd}")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(import_pr.subprocess, "run", _guarded_run)

    rc = import_pr.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "kb-import-pr: REFUSED — " in out
    assert ("the manifest remote is a fork-facing placeholder, not a trusted source of "
            "patches") in out
