import subprocess

from kb_core.publish import cli, lock, state, verify
from tests.publish_fakes import PUBLIC_URL, default_github, gated, wire_public

MANIFEST = """
[publish]
remote = "{remote}"
[export]
include = ["src/**", "publish.manifest.toml"]
"""
DENY = '[deny]\npatterns = ["secretname"]\n'


def _repo(tmp_path, monkeypatch):
    """A private repo with a github.com origin and a pinned public mirror.

    The mirror (`remote`, a local bare repo) is pushed to as PUBLIC_URL through
    a fake GIT_SSH_COMMAND — kb-publish --push refuses any target that is not
    the pinned github.com repo.
    """
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "publish.deny.toml").write_text(DENY)
    remote, _ = wire_public(tmp_path, root, monkeypatch)
    # Dry runs clone the manifest remote: keep it local so no test touches the network.
    (root / "publish.manifest.toml").write_text(MANIFEST.format(remote=remote))
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    _approve(root)
    return root, remote


def _approve(root):
    """Record HEAD's export in the path lockfile and commit it: kb-publish
    refuses any path not approved at the ref it publishes."""
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    subprocess.run(["git", "add", lock.LOCK_REL], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "approve paths"], cwd=root, check=True)


def _remote_refs(remote):
    return subprocess.run(["git", "show-ref"], cwd=remote,
                          capture_output=True, text=True).stdout


def test_dry_run_publishes_nothing(tmp_path, monkeypatch):
    root, remote = _repo(tmp_path, monkeypatch)
    rc = cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True)
    assert rc == 0
    assert _remote_refs(remote).strip() == ""


def test_push_publishes(tmp_path, monkeypatch):
    # The scratch manifest's remote is a manifest-fallback value
    # (not --remote/env), and push=True is refused unless an explicit remote
    # is supplied — pass the bare repo path directly so the push it exercises
    # still happens.
    root, remote = _repo(tmp_path, monkeypatch)
    rc = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root),
                 remote=PUBLIC_URL, github=default_github())
    assert rc == 0
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"],
                           cwd=remote, capture_output=True, text=True).stdout.split()
    assert "src/a.py" in files and "publish.deny.toml" not in files


def test_planted_token_refused(tmp_path, monkeypatch, capsys):
    root, remote = _repo(tmp_path, monkeypatch)
    (root / "src" / "oops.py").write_text("# secretname was here\n")
    subprocess.run(["git", "add", "src/oops.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "oops"], cwd=root, check=True)
    rc = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root))
    assert rc == 1
    assert "denylist/structural hit" in capsys.readouterr().out
    assert _remote_refs(remote).strip() == ""  # NOTHING was pushed


def test_env_remote_overrides_manifest(tmp_path, monkeypatch):
    # Manifest's [publish] remote is a bogus/unreachable path; KB_PUBLISH_REMOTE
    # points at the real bare repo. The push must land in the real repo, proving
    # env beats the manifest fallback (precedence: --remote flag > env > manifest).
    root, remote = _repo(tmp_path, monkeypatch)
    bogus = tmp_path / "does-not-exist.git"
    (root / "publish.manifest.toml").write_text(MANIFEST.format(remote=bogus))
    subprocess.run(["git", "add", "publish.manifest.toml"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "bogus manifest remote"], cwd=root, check=True)
    _approve(root)   # the manifest is a pinned *.toml: its new bytes need approval

    monkeypatch.setenv("KB_PUBLISH_REMOTE", PUBLIC_URL)
    rc = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root),
                 github=default_github())
    assert rc == 0
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"],
                           cwd=remote, capture_output=True, text=True).stdout.split()
    assert "src/a.py" in files


def test_manifest_fallback_remote_refuses_push(tmp_path, monkeypatch, capsys):
    # --push with no --remote/KB_PUBLISH_REMOTE must refuse — the
    # manifest remote is a fork-facing placeholder, not something to push to.
    root, remote = _repo(tmp_path, monkeypatch)
    st = state.load(root)       # with a pin the push would go to the pinned repo
    st.update(public_repo_id=None, public_repo_full_name=None)
    state.save(root, st)
    rc = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root))
    assert rc == 1
    assert "placeholder" in capsys.readouterr().out
    assert _remote_refs(remote).strip() == ""


def test_verify_failure_refused(tmp_path, monkeypatch):
    root, remote = _repo(tmp_path, monkeypatch)

    def _boom(tree, **kw):
        raise verify.VerifyError("fake-step", "boom")

    # verify=None: the real seam, i.e. cli.verify.verify — patched to fail.
    monkeypatch.setattr(cli.verify, "verify", _boom)
    rc = cli.run(root, ref="HEAD", push=True, tag=None, remote=PUBLIC_URL,
                 github=default_github(), **gated(root, verify=None))
    assert rc == 2
    assert _remote_refs(remote).strip() == ""


def test_missing_deny_file_refused(tmp_path, monkeypatch):
    root, remote = _repo(tmp_path, monkeypatch)
    (root / "publish.deny.toml").unlink()
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "drop denylist"], cwd=root, check=True)
    rc = cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True)
    assert rc == 1
    assert _remote_refs(remote).strip() == ""


def test_empty_denylist_refused(tmp_path, monkeypatch):
    root, remote = _repo(tmp_path, monkeypatch)
    (root / "publish.deny.toml").write_text("[deny]\npatterns = []\n")
    subprocess.run(["git", "add", "publish.deny.toml"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "empty denylist"], cwd=root, check=True)
    rc = cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True)
    assert rc == 1
    assert _remote_refs(remote).strip() == ""


def test_deny_file_diverges_from_ref_refused(tmp_path, monkeypatch):
    root, remote = _repo(tmp_path, monkeypatch)
    # Uncommitted working-tree edit to the deny file: the committed ref still
    # carries the original patterns, so the export tree was screened against
    # different content than what's on disk right now.
    (root / "publish.deny.toml").write_text('[deny]\npatterns = ["secretname", "other"]\n')
    rc = cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True)
    assert rc == 1
    assert _remote_refs(remote).strip() == ""


def test_identity_hit_refused_and_overridable(tmp_path, monkeypatch):
    root, remote = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_NAME", "secretname")

    rc = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root),
                 remote=PUBLIC_URL, github=default_github())
    assert rc == 1
    assert _remote_refs(remote).strip() == ""

    rc2 = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root),
                  remote=PUBLIC_URL, github=default_github(), allow_identity_hit=True)
    assert rc2 == 0
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"],
                           cwd=remote, capture_output=True, text=True).stdout.split()
    assert "src/a.py" in files


def test_tag_denylist_hit_refused_and_not_overridable(tmp_path, monkeypatch):
    # Tags land in immutable public metadata the same as the commit
    # message/coauthors — a denylisted --tag must refuse the same way, and
    # --allow-identity-hit (identity-only) must not wave it through.
    root, remote = _repo(tmp_path, monkeypatch)
    rc = cli.run(root, ref="HEAD", push=True, tag="secretname-v1",
                 **gated(root), remote=PUBLIC_URL, github=default_github())
    assert rc == 1
    assert _remote_refs(remote).strip() == ""  # nothing pushed

    rc2 = cli.run(root, ref="HEAD", push=True, tag="secretname-v1",
                  **gated(root), remote=PUBLIC_URL, github=default_github(), allow_identity_hit=True)
    assert rc2 == 1
    assert _remote_refs(remote).strip() == ""  # still refused


def test_push_records_state_and_clears_coauthors(tmp_path, monkeypatch):
    root, remote = _repo(tmp_path, monkeypatch)
    st = state.load(root)          # keeps the pin wire_public recorded
    st["pending_coauthors"] = [{"name": "Alice", "email": "alice@example.com"}]
    state.save(root, st)
    rc = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root),
                 remote=PUBLIC_URL, github=default_github())
    assert rc == 0
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                         capture_output=True, text=True).stdout.strip()
    st = state.load(root)
    assert st["last_published_ref"] == sha
    assert st["pending_coauthors"] == []


def test_unapproved_media_refused(tmp_path, monkeypatch):
    root, remote = _repo(tmp_path, monkeypatch)
    (root / "src" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    subprocess.run(["git", "add", "src/shot.png"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "shot"], cwd=root, check=True)
    rc = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root),
                 remote=PUBLIC_URL, github=default_github())
    assert rc == 1
    assert _remote_refs(remote).strip() == ""  # NOTHING was pushed


def _commit_file(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    subprocess.run(["git", "add", rel], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", f"add {rel}"], cwd=root, check=True)


import pytest  # noqa: E402


@pytest.mark.parametrize("rel,data", [
    ("src/secretname_notes.md", "clean body\n"),          # path name
    ("src/notes.txt", "secretname\n".encode("utf-16")),   # encoding
    ("src/bundle.zip", b"PK\x03\x04"),                    # archive
    ("src/c.md", "mail jane" + "@gmail.com\n"),          # email domain (split: this
    ("src/p.md", "cd ~/Desk" + "top/proj\n"),            # file is scanned too)
])
def test_structural_hit_refused(tmp_path, monkeypatch, capsys, rel, data):
    root, remote = _repo(tmp_path, monkeypatch)
    _commit_file(root, rel, data)
    rc = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root),
                 remote=PUBLIC_URL, github=default_github())
    assert rc == 1
    assert "REFUSED" in capsys.readouterr().out
    assert _remote_refs(remote).strip() == ""


def test_lint_warning_does_not_block(tmp_path, monkeypatch, capsys):
    root, remote = _repo(tmp_path, monkeypatch)
    _commit_file(root, "src/tz.py", "TZ = 'Americ" + "a/Denver'\n")
    _approve(root)
    rc = cli.run(root, ref="HEAD", push=True, tag=None, **gated(root),
                 remote=PUBLIC_URL, github=default_github())
    assert rc == 0
    out = capsys.readouterr().out
    assert "warning" in out.lower() and "src/tz.py" in out
