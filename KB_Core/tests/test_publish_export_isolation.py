"""export.assemble publishes the COMMITTED bytes, whatever attributes say.

`git archive` applies gitattributes — export-subst, export-ignore, eol/text
conversion — from the archived tree AND from untracked sources
($GIT_DIR/info/attributes, core.attributesFile, the system file). Any of them
can make the published bytes differ from what was reviewed, approved and
hash-pinned. And its tar of an absolute symlink made extraction abort, so one
stray link anywhere in the repo blocked every publish.
"""
import os
import stat
import subprocess

import pytest

from kb_core.publish import export

MANIFEST = """
[publish]
remote = "https://github.com/example/pub.git"
[export]
include = ["src/**", "publish.manifest.toml", ".gitattributes"]
"""

# Assembled so this file (exported itself) never holds the placeholder.
PLACEHOLDER = "$" + "Format:%H$"


def _git(root, *args, **kw):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, **kw)


def _repo(tmp_path, files=None):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "publish.manifest.toml").write_text(MANIFEST)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_bytes(b"a = 1\nb = 2\n")
    for rel, data in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _blob(root, rel):
    return _git(root, "cat-file", "blob", f"HEAD:{rel}").stdout


def test_untracked_info_attributes_cannot_rewrite_exported_bytes(tmp_path):
    root = _repo(tmp_path)
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "attributes").write_text("*.py text eol=crlf\n")
    dest = tmp_path / "out"
    export.assemble(root, "HEAD", dest)
    assert (dest / "src" / "a.py").read_bytes() == _blob(root, "src/a.py")


def test_untracked_info_attributes_cannot_drop_a_selected_file(tmp_path):
    root = _repo(tmp_path)
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "attributes").write_text("src/a.py export-ignore\n")
    kept = export.assemble(root, "HEAD", tmp_path / "out")
    assert "src/a.py" in kept


def test_global_attributes_file_is_ignored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    attrs = home / "attrs"
    attrs.write_text("*.py text eol=crlf\n")
    (home / ".gitconfig").write_text(f"[core]\n\tattributesFile = {attrs}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    root = _repo(tmp_path)
    dest = tmp_path / "out"
    export.assemble(root, "HEAD", dest)
    assert (dest / "src" / "a.py").read_bytes() == _blob(root, "src/a.py")


def test_in_tree_gitattributes_cannot_rewrite_or_drop(tmp_path):
    root = _repo(tmp_path, {".gitattributes": b"*.py text eol=crlf\nsrc/b.py export-ignore\n",
                            "src/b.py": b"b = 2\n"})
    dest = tmp_path / "out"
    kept = export.assemble(root, "HEAD", dest)
    assert "src/b.py" in kept
    assert (dest / "src" / "a.py").read_bytes() == _blob(root, "src/a.py")


def test_format_placeholder_is_refused_and_dest_removed(tmp_path):
    root = _repo(tmp_path, {"src/v.py": f"VERSION = '{PLACEHOLDER}'\n".encode()})
    dest = tmp_path / "out"
    with pytest.raises(export.ExportError) as e:
        export.assemble(root, "HEAD", dest)
    assert "src/v.py" in str(e.value) and "export-subst" in str(e.value)
    assert not dest.exists()


def test_format_placeholder_outside_the_selection_is_not_our_business(tmp_path):
    root = _repo(tmp_path, {"notes/v.txt": PLACEHOLDER.encode()})
    kept = export.assemble(root, "HEAD", tmp_path / "out")
    assert "notes/v.txt" not in kept


def test_absolute_symlink_no_longer_aborts_the_export(tmp_path, capsys):
    root = _repo(tmp_path)
    os.symlink("/etc/hosts", root / "src" / "abs")
    os.symlink("../../outside", root / "src" / "rel_escape")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "links")
    dest = tmp_path / "out"
    kept = export.assemble(root, "HEAD", dest)
    assert kept == ["publish.manifest.toml", "src/a.py"]
    assert not os.path.lexists(dest / "src" / "abs")
    assert not os.path.lexists(dest / "src" / "rel_escape")
    out = capsys.readouterr().out
    assert "stripped symlink src/abs" in out and "stripped symlink src/rel_escape" in out


def test_executable_bit_survives(tmp_path):
    root = _repo(tmp_path, {"src/run.sh": b"#!/bin/sh\necho ok\n"})
    os.chmod(root / "src" / "run.sh", 0o755)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "exec")
    dest = tmp_path / "out"
    export.assemble(root, "HEAD", dest)
    assert os.stat(dest / "src" / "run.sh").st_mode & stat.S_IXUSR
    assert not os.stat(dest / "src" / "a.py").st_mode & stat.S_IXUSR


def test_inherited_git_dir_does_not_redirect_the_source(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    kept = export.assemble(root, "HEAD", tmp_path / "out")
    assert "src/a.py" in kept


def test_every_exported_file_is_the_committed_blob(tmp_path):
    root = _repo(tmp_path, {"src/bin.dat": bytes(range(256)) * 4,
                            "src/crlf.txt": b"one\r\ntwo\r\n"})
    dest = tmp_path / "out"
    kept = export.assemble(root, "HEAD", dest)
    for rel in kept:
        assert (dest / rel).read_bytes() == _blob(root, rel), rel


def test_bad_ref_is_an_export_error(tmp_path):
    root = _repo(tmp_path)
    dest = tmp_path / "out"
    with pytest.raises(export.ExportError):
        export.assemble(root, "no-such-ref", dest)
    assert not dest.exists()


def _commit_blob_paths(root, mapping):
    """Commit paths that may collide on a case-insensitive filesystem, built
    with plumbing so the host's filesystem never has to hold both."""
    for rel, data in mapping.items():
        oid = _git(root, "hash-object", "-w", "--stdin", input=data).stdout.decode().strip()
        _git(root, "update-index", "--add", "--cacheinfo", f"100644,{oid},{rel}")
    _git(root, "commit", "-qm", "case")


def test_selected_paths_differing_only_in_case_are_refused(tmp_path):
    root = _repo(tmp_path)
    _commit_blob_paths(root, {"src/Readme.md": b"one\n", "src/README.md": b"two\n"})
    dest = tmp_path / "out"
    with pytest.raises(export.ExportError) as e:
        export.assemble(root, "HEAD", dest)
    assert "case" in str(e.value) and "src/README.md" in str(e.value)
    assert not dest.exists()


def test_unselected_case_twin_cannot_overwrite_a_selected_file(tmp_path):
    root = _repo(tmp_path)
    _commit_blob_paths(root, {"SRC/a.py": b"secret = 1\n"})     # not selected (case-sensitive glob)
    dest = tmp_path / "out"
    try:
        kept = export.assemble(root, "HEAD", dest)
    except export.ExportError as e:
        assert "case" in str(e)
        return
    assert "src/a.py" in kept
    assert (dest / "src" / "a.py").read_bytes() == b"a = 1\nb = 2\n"
