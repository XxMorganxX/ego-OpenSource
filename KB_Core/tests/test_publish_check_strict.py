"""kb-check-publishable --strict: the private-repo guard fails CLOSED.

Non-strict stays a loud skip when there is no deny file, because the public
repo (and every fork) legitimately has none. Strict is what private CI runs:
there, a missing/empty denylist, a gutted selection or a denylist that no
longer catches the out-of-band canaries is a refusal, never a pass.
"""
import subprocess

import pytest

from kb_core.publish import check, lock

MANIFEST = """
[publish]
remote = "https://github.com/example/pub.git"
[export]
include = ["src/**"]
"""


def _repo(tmp_path, deny='[deny]\npatterns = ["secretname", "widget ?co"]\n', n_files=1):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "publish.manifest.toml").write_text(MANIFEST)
    if deny is not None:
        (tmp_path / "publish.deny.toml").write_text(deny)
    (tmp_path / "src").mkdir()
    files = [f"src/f{i}.py" for i in range(n_files)]
    for rel in files:
        (tmp_path / rel).write_text("x = 1\n")
    # Every selected path approved — strict refuses without a lockfile.
    (tmp_path / "private").mkdir()
    (tmp_path / lock.LOCK_REL).write_text(lock.render(lock.snapshot(tmp_path, files)))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PUBLISH_DENY_CANARIES", raising=False)
    monkeypatch.delenv("KB_PUBLISH_GUARD_REQUIRED", raising=False)


def _main(root, monkeypatch, *argv):
    monkeypatch.chdir(root)
    return check.main(list(argv))


def test_nonstrict_missing_deny_is_a_loud_skip(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, deny=None)
    assert _main(root, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "NOT a personal-data scan" in out


def test_strict_missing_deny_refused(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, deny=None)
    assert _main(root, monkeypatch, "--strict", "--min-selected", "1") == 1
    assert "REFUSED" in capsys.readouterr().out


def test_guard_required_env_implies_strict(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, deny=None)
    monkeypatch.setenv("KB_PUBLISH_GUARD_REQUIRED", "1")
    assert _main(root, monkeypatch) == 1
    assert "REFUSED" in capsys.readouterr().out


@pytest.mark.parametrize("strict", [True, False])
def test_zero_patterns_refused(tmp_path, monkeypatch, capsys, strict):
    # A deny file that exists but is empty is never "clean" — in either mode.
    root = _repo(tmp_path, deny="[deny]\npatterns = []\n")
    monkeypatch.setenv("PUBLISH_DENY_CANARIES", "x")
    argv = ["--strict", "--min-selected", "1"] if strict else []
    assert _main(root, monkeypatch, *argv) == 1
    assert "REFUSED" in capsys.readouterr().out


@pytest.mark.parametrize("strict", [True, False])
def test_zero_selection_refused(tmp_path, monkeypatch, capsys, strict):
    root = _repo(tmp_path)
    (root / "publish.manifest.toml").write_text(MANIFEST.replace("src/**", "nothing/**"))
    monkeypatch.setenv("PUBLISH_DENY_CANARIES", "secretname")
    argv = ["--strict", "--min-selected", "1"] if strict else []
    assert _main(root, monkeypatch, *argv) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_strict_selection_below_floor_refused(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, n_files=3)
    monkeypatch.setenv("PUBLISH_DENY_CANARIES", "secretname")
    assert _main(root, monkeypatch, "--strict", "--min-selected", "4") == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "3" in out


def test_strict_default_floor_is_meaningful():
    # Guards a gutted manifest; today's real export is ~700 paths.
    assert check.STRICT_MIN_SELECTED >= 100


def test_strict_all_canaries_matched_passes(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    monkeypatch.setenv("PUBLISH_DENY_CANARIES", "SecretName\nWidget Co, widgetco\n")
    assert _main(root, monkeypatch, "--strict", "--min-selected", "1") == 0
    out = capsys.readouterr().out
    assert "3/3" in out
    for c in ("SecretName", "Widget Co", "widgetco"):
        assert c not in out


def test_strict_unmatched_canary_refused_without_printing_it(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    monkeypatch.setenv("PUBLISH_DENY_CANARIES", "secretname,zebra-quartz-77")
    assert _main(root, monkeypatch, "--strict", "--min-selected", "1") == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "1/2" in out
    assert "zebra" not in out.lower() and "secretname" not in out.lower()


def test_strict_without_canaries_refused(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    assert _main(root, monkeypatch, "--strict", "--min-selected", "1") == 1
    assert "PUBLISH_DENY_CANARIES" in capsys.readouterr().out


def test_nonstrict_checks_canaries_when_given(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("PUBLISH_DENY_CANARIES", "zebra-quartz-77")
    assert _main(root, monkeypatch) == 1


def test_strict_hits_still_fail(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    (root / "src" / "bad.py").write_text("# secretname\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    monkeypatch.setenv("PUBLISH_DENY_CANARIES", "secretname")
    assert _main(root, monkeypatch, "--strict", "--min-selected", "1") == 1


def test_invalid_manifest_refused(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    (root / "publish.manifest.toml").write_text(MANIFEST + 'excludes = ["src/x"]\n')
    assert _main(root, monkeypatch) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_parse_canaries():
    assert check.parse_canaries(" a ,b\n\nc,, \n") == ["a", "b", "c"]
    assert check.parse_canaries(None) == []
