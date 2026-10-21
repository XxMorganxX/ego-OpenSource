import subprocess

from kb_core.publish import check

MANIFEST = """
[publish]
remote = "https://github.com/example/pub.git"
[export]
include = ["src/**"]
"""
DENY = """
[deny]
patterns = ["secretname"]
"""


def _repo(tmp_path, with_deny=True):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "publish.manifest.toml").write_text(MANIFEST)
    if with_deny:
        (tmp_path / "publish.deny.toml").write_text(DENY)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("x = 1\n")
    (tmp_path / "notes.md").write_text("secretname lives here\n")  # outside manifest
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_clean_tree_no_hits(tmp_path):
    root = _repo(tmp_path)
    assert check.run_check(root) == []


def test_hit_inside_manifest_path(tmp_path):
    root = _repo(tmp_path)
    (root / "src" / "bad.py").write_text("# ping secretname\n")
    subprocess.run(["git", "add", "src/bad.py"], cwd=root, check=True)
    hits = check.run_check(root)
    assert [h.path for h in hits] == ["src/bad.py"]


def test_hit_outside_manifest_is_ignored(tmp_path):
    root = _repo(tmp_path)
    # notes.md contains the token but is not in the include set
    assert check.run_check(root) == []


def test_missing_denylist_skips(tmp_path):
    root = _repo(tmp_path, with_deny=False)
    assert check.run_check(root) == []


def test_media_outside_allowlist_is_a_hit(tmp_path):
    # Images are never denylist-scanned (a screenshot can show real corpus
    # names no pattern matches), so any exported media must be allowlisted.
    root = _repo(tmp_path)
    (root / "src" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    subprocess.run(["git", "add", "src/shot.png"], cwd=root, check=True)
    hits = check.run_check(root)
    assert [(h.path, h.line_no) for h in hits] == [("src/shot.png", 0)]


def test_allowlisted_media_passes(tmp_path):
    root = _repo(tmp_path)
    (root / "publish.manifest.toml").write_text(
        MANIFEST + 'allow_media = ["src/logo.png"]\n')
    (root / "src" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    assert check.run_check(root) == []


def _add(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    subprocess.run(["git", "add", rel], cwd=root, check=True)


def test_path_name_hit(tmp_path):
    root = _repo(tmp_path)
    _add(root, "src/secretname_notes.md", "clean body\n")
    assert [(h.path, h.line_no) for h in check.run_check(root)] == [("src/secretname_notes.md", 0)]


def test_structural_hits_block(tmp_path):
    root = _repo(tmp_path)
    _add(root, "src/u16.txt", "hello\n".encode("utf-16"))
    _add(root, "src/contact.md", "write to jane" + "@gmail.com\n")
    _add(root, "src/bundle.zip", b"PK\x03\x04")
    assert sorted(h.path for h in check.run_check(root)) == [
        "src/bundle.zip", "src/contact.md", "src/u16.txt"]


def test_lint_is_reported_but_not_a_hit(tmp_path):
    root = _repo(tmp_path)
    _add(root, "src/tz.py", "TZ = 'Americ" + "a/Denver'\n")
    assert check.run_check(root) == []
    rep = check.inspect(root)
    assert [w.path for w in rep.warnings] == ["src/tz.py"]
    assert rep.selected == 2


def test_main_prints_warnings_and_passes(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _add(root, "src/tz.py", "TZ = 'Americ" + "a/Denver'\n")
    monkeypatch.delenv("PUBLISH_DENY_CANARIES", raising=False)
    monkeypatch.delenv("KB_PUBLISH_GUARD_REQUIRED", raising=False)
    monkeypatch.chdir(root)
    assert check.main([]) == 0
    out = capsys.readouterr().out
    assert "warning" in out.lower() and "src/tz.py" in out
