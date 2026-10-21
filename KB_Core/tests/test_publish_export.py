import subprocess

import pytest

from kb_core.publish import export

MANIFEST = """
[publish]
remote = "https://github.com/example/pub.git"
[export]
include = ["src/**", "publish.manifest.toml"]
"""


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "publish.manifest.toml").write_text(MANIFEST)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n")
    (root / "private.txt").write_text("keep out\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def test_assemble_keeps_only_manifest_paths(tmp_path):
    root = _repo(tmp_path)
    dest = tmp_path / "out"
    kept = export.assemble(root, "HEAD", dest)
    assert kept == ["publish.manifest.toml", "src/a.py"]
    assert (dest / "src" / "a.py").exists()
    assert not (dest / "private.txt").exists()


def test_assemble_uncommitted_changes_not_exported(tmp_path):
    root = _repo(tmp_path)
    (root / "src" / "b.py").write_text("b = 2\n")  # never committed
    dest = tmp_path / "out"
    kept = export.assemble(root, "HEAD", dest)
    assert "src/b.py" not in kept


def test_assemble_empty_selection_raises(tmp_path):
    root = _repo(tmp_path)
    (root / "publish.manifest.toml").write_text(MANIFEST.replace('"src/**", ', ""))
    subprocess.run(["git", "commit", "-aqm", "narrow"], cwd=root, check=True)
    (root / "publish.manifest.toml").write_text(
        MANIFEST.replace("src/**", "nothing/**").replace('"publish.manifest.toml"', '"nope"'))
    subprocess.run(["git", "commit", "-aqm", "empty"], cwd=root, check=True)
    with pytest.raises(export.ExportError):
        export.assemble(root, "HEAD", tmp_path / "out2")


def test_assemble_refuses_nonempty_dest(tmp_path):
    root = _repo(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    seeded_file = dest / "leftover_secret.py"
    seeded_file.write_text("secret = True\n")
    with pytest.raises(export.ExportError):
        export.assemble(root, "HEAD", dest)
    # Refusal must not delete the caller's data
    assert seeded_file.exists()
    assert seeded_file.read_text() == "secret = True\n"


def test_assemble_cleans_dest_on_failure(tmp_path):
    root = _repo(tmp_path)
    (root / "publish.manifest.toml").write_text(MANIFEST.replace('"src/**", ', ""))
    subprocess.run(["git", "commit", "-aqm", "narrow"], cwd=root, check=True)
    (root / "publish.manifest.toml").write_text(
        MANIFEST.replace("src/**", "nothing/**").replace('"publish.manifest.toml"', '"nope"'))
    subprocess.run(["git", "commit", "-aqm", "empty"], cwd=root, check=True)
    dest = tmp_path / "out3"
    with pytest.raises(export.ExportError):
        export.assemble(root, "HEAD", dest)
    # On failure, dest must be removed entirely
    assert not dest.exists()


def test_assemble_strips_symlinks(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "publish.manifest.toml").write_text(MANIFEST)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n")
    # Create a symlink and commit it
    (root / "src" / "link.py").symlink_to("a.py")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init with symlink"], cwd=root, check=True)
    dest = tmp_path / "out"
    kept = export.assemble(root, "HEAD", dest)
    # Symlink must not be in kept list and must not exist on disk
    assert "src/link.py" not in kept
    assert not (dest / "src" / "link.py").exists()
    # Regular files survive
    assert "src/a.py" in kept
    assert (dest / "src" / "a.py").exists()


def test_assemble_never_writes_an_unselected_file(tmp_path, monkeypatch):
    """Selection happens on the path list: a process killed mid-export (SIGKILL,
    OOM, a runner timeout — no Python cleanup) can only leave selected files."""
    root = _repo(tmp_path)
    dest = tmp_path / "out"
    written = []
    real = export._write_blobs

    def spy(root_, dest_, entries):
        written.extend(rel for _, _, rel in entries)
        real(root_, dest_, entries)
        on_disk = sorted(str(p.relative_to(dest_)) for p in dest_.rglob("*") if p.is_file())
        assert on_disk == ["publish.manifest.toml", "src/a.py"]

    monkeypatch.setattr(export, "_write_blobs", spy)
    export.assemble(root, "HEAD", dest)
    assert sorted(written) == ["publish.manifest.toml", "src/a.py"]


def test_assemble_reads_the_manifest_even_when_it_is_not_exported(tmp_path):
    root = _repo(tmp_path)
    (root / "publish.manifest.toml").write_text(MANIFEST.replace(', "publish.manifest.toml"', ""))
    subprocess.run(["git", "commit", "-aqm", "manifest stays private"], cwd=root, check=True)
    dest = tmp_path / "out"
    assert export.assemble(root, "HEAD", dest) == ["src/a.py"]
    assert not (dest / "publish.manifest.toml").exists()
    assert not (dest / "private.txt").exists()


def test_assemble_without_a_manifest_at_ref_raises(tmp_path):
    root = _repo(tmp_path)
    subprocess.run(["git", "rm", "-q", "publish.manifest.toml"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "no manifest"], cwd=root, check=True)
    with pytest.raises(export.ExportError, match="not present"):
        export.assemble(root, "HEAD", tmp_path / "out")
    assert not (tmp_path / "out").exists()
