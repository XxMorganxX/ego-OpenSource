"""load_manifest refuses manifests whose patterns can't mean what they say.

fnmatchcase never matches './x', '/x' or 'a/../b' against git's repo-relative
paths, so such a pattern is a silent no-op: an exclude that excludes nothing
fails OPEN. Unknown keys are refused for the same reason — `excludes =`
(typo) would otherwise be ignored and every path it meant to hold back exported.
"""
import pytest

from kb_core.publish import manifest

BASE = """
[publish]
remote = "https://github.com/example/pub.git"

[export]
include = {include}
exclude = {exclude}
"""


def _load(tmp_path, include='["src/**"]', exclude="[]", extra=""):
    p = tmp_path / "publish.manifest.toml"
    p.write_text(BASE.format(include=include, exclude=exclude) + extra)
    return manifest.load_manifest(p)


def test_valid_manifest_loads(tmp_path):
    m = _load(tmp_path, exclude='["src/private_ok.py"]')
    assert m.include == ["src/**"]


@pytest.mark.parametrize("pat", [
    "./src/**",          # leading ./ never matches a git path
    "/src/**",           # absolute
    "src/../private/**", # parent traversal
    "..",
    "",
    "src\\\\win.py",     # backslash: never a git path separator
])
@pytest.mark.parametrize("where", ["include", "exclude"])
def test_bad_patterns_refused(tmp_path, pat, where):
    lst = f'["{pat}"]'
    kwargs = {"include": lst} if where == "include" else {"exclude": lst}
    with pytest.raises(manifest.ManifestError):
        _load(tmp_path, **kwargs)


def test_bad_allow_media_pattern_refused(tmp_path):
    p = tmp_path / "publish.manifest.toml"
    p.write_text(BASE.format(include='["src/**"]', exclude="[]")
                 .replace("exclude = []", 'exclude = []\nallow_media = ["./logo.png"]'))
    with pytest.raises(manifest.ManifestError):
        manifest.load_manifest(p)


def test_case_variant_duplicates_refused(tmp_path):
    # One of these is a typo for the other; on a case-insensitive Mac both
    # name the same files, but fnmatchcase only honours one of them.
    with pytest.raises(manifest.ManifestError, match="case"):
        _load(tmp_path, exclude='["**/CLAUDE.md", "**/claude.md"]')


def test_case_variant_across_include_and_exclude_refused(tmp_path):
    with pytest.raises(manifest.ManifestError, match="case"):
        _load(tmp_path, include='["Docs/**"]', exclude='["docs/**"]')


def test_unknown_export_key_refused(tmp_path):
    p = tmp_path / "publish.manifest.toml"
    p.write_text(BASE.format(include='["src/**"]', exclude="[]")
                 .replace("exclude = []", 'excludes = ["src/secret.py"]'))
    with pytest.raises(manifest.ManifestError, match="excludes"):
        manifest.load_manifest(p)


def test_unknown_table_refused(tmp_path):
    with pytest.raises(manifest.ManifestError, match="extra"):
        _load(tmp_path, extra='\n[extra]\nx = 1\n')


def test_unknown_publish_key_refused(tmp_path):
    p = tmp_path / "publish.manifest.toml"
    p.write_text(BASE.format(include='["src/**"]', exclude="[]")
                 .replace('[publish]\n', '[publish]\nbranch = "dev"\n'))
    with pytest.raises(manifest.ManifestError, match="branch"):
        manifest.load_manifest(p)


def test_missing_or_empty_include_refused(tmp_path):
    with pytest.raises(manifest.ManifestError):
        _load(tmp_path, include="[]")


def test_non_string_pattern_refused(tmp_path):
    with pytest.raises(manifest.ManifestError):
        _load(tmp_path, include='["src/**", 3]')


def test_manifest_error_is_a_value_error():
    # callers that already catch ValueError keep failing closed
    assert issubclass(manifest.ManifestError, ValueError)


def test_export_refuses_invalid_manifest_at_ref(tmp_path):
    import subprocess

    from kb_core.publish import export
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "publish.manifest.toml").write_text(
        BASE.format(include='["src/**", "publish.manifest.toml"]', exclude="[]")
        .replace("exclude = []", 'excludes = ["src/secret.py"]'))
    (root / "src").mkdir()
    (root / "src" / "secret.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    with pytest.raises(export.ExportError, match="excludes"):
        export.assemble(root, "HEAD", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_cli_prints_refused_on_invalid_manifest(tmp_path, capsys):
    import subprocess

    from kb_core.publish import cli
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "publish.manifest.toml").write_text(
        BASE.format(include='["./src/**", "publish.manifest.toml"]', exclude="[]"))
    (root / "publish.deny.toml").write_text('[deny]\npatterns = ["secretname"]\n')
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    rc = cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True)
    assert rc == 1
    assert "REFUSED" in capsys.readouterr().out
