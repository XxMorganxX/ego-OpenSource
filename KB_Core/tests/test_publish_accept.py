"""`kb-publish accept`: the post-publish acceptance scan, as a command.

Acceptance means: fresh-clone the public repo and run the REAL deny file plus the
structural detectors over the clone. Nothing did that — kb-check-publishable
in a public clone skips (no deny file), and with one copied in it scans only
manifest-selected files at HEAD, never a file outside the selection, never
older commits, never commit or tag objects. accept clones (read-only), then
scans every file at public HEAD, every blob anywhere in its history (the
denylist may have grown since), and every commit and tag object.
"""
import subprocess

from kb_core.publish import cli
from tests.publish_fakes import MIRROR, commit, gated_push, git, push_origin, scratch_private


def _accept(root, remote, capsys):
    capsys.readouterr()
    rc = cli.accept(root, remote=str(remote))
    return rc, capsys.readouterr().out


def _forge(tmp_path, remote, files, subject="sync: 2026-01-01 (upstream 0000000)"):
    work = tmp_path / f"forger{len(list(tmp_path.glob('forger*')))}"
    git(tmp_path, "clone", "-q", str(remote), str(work))
    for rel, text in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    ident = ("-c", f"user.name={MIRROR[0]}", "-c", f"user.email={MIRROR[1]}")
    git(work, *ident, "add", "-A")
    git(work, *ident, "commit", "-qm", subject)
    git(work, "push", "-q", "origin", "main")
    return work


def test_clean_public_repo_accepted(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh, tag="v0.1.0") == 0
    rc, out = _accept(root, remote, capsys)
    assert rc == 0 and "scanned clean" in out and "1 tag" in out


def test_file_outside_the_manifest_selection_is_scanned(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    _forge(tmp_path, remote, {"other/leak.txt": "notes on secretname\n"})
    rc, out = _accept(root, remote, capsys)
    assert rc == 1 and "REFUSED" in out and "other/leak.txt" in out


def test_history_is_scanned_against_todays_denylist(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch,
                                       files={"src/b.py": "who = 'zorblax'\n"})
    assert gated_push(root, gh) == 0
    commit(root, "drop b", **{"src/a.py": "a = 2\n"})
    git(root, "rm", "-q", "src/b.py")
    commit(root, "rm b")
    push_origin(root)
    assert gated_push(root, gh) == 0
    # Only now is the name denylisted: HEAD is clean, the first sync commit isn't.
    (root / "publish.deny.toml").write_text('[deny]\npatterns = ["secretname", "zorblax"]\n')
    rc, out = _accept(root, remote, capsys)
    assert rc == 1 and "src/b.py" in out and "history" in out


def test_tag_and_commit_objects_are_scanned(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    work = tmp_path / "tagger"
    git(tmp_path, "clone", "-q", str(remote), str(work))
    git(work, "-c", "user.name=x", "-c", "user.email=x@example.com", "tag", "-a", "v9",
        "-m", "for secretname")
    git(work, "push", "-q", "origin", "v9")
    rc, out = _accept(root, remote, capsys)
    assert rc == 1 and "tag object" in out


def test_personal_email_in_a_commit_object_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    work = tmp_path / "c"
    git(tmp_path, "clone", "-q", str(remote), str(work))
    (work / "src" / "z.py").write_text("z = 1\n")
    git(work, "add", "-A")
    git(work, "-c", "user.name=Someone", "-c", "user.email=someone" + "@" + "realmail.com",
        "commit", "-qm", "edit")
    git(work, "push", "-q", "origin", "main")
    rc, out = _accept(root, remote, capsys)
    assert rc == 1 and "commit object" in out and "email" in out


def test_pull_request_refs_are_not_fetched(tmp_path, monkeypatch, capsys):
    """GitHub advertises refs/pull/*: a contributor's head and GitHub's merge
    commit, by other people, never published by kb-publish. They must not turn
    acceptance red (personal email, a denylisted word, a non-sync subject)."""
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh, tag="v0.1.0") == 0
    work = tmp_path / "contributor"
    git(tmp_path, "clone", "-q", str(remote), str(work))
    (work / "src" / "pr.py").write_text("by = 'secretname'\n")
    git(work, "add", "-A")
    git(work, "-c", "user.name=Contributor", "-c", "user.email=jane" + "@" + "realmail.com",
        "commit", "-qm", "my PR mentions secretname")
    git(work, "push", "-q", "origin", "HEAD:refs/pull/1/head")
    git(work, "push", "-q", "origin", "HEAD:refs/pull/1/merge")
    assert "refs/pull/1/head" in git(remote, "show-ref")
    rc, out = _accept(root, remote, capsys)
    assert rc == 0 and "scanned clean" in out and "1 commit(s)" in out and "1 tag" in out


def test_accept_never_writes_the_remote(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    before = subprocess.run(["git", "show-ref"], cwd=remote, capture_output=True,
                            text=True).stdout
    _accept(root, remote, capsys)
    assert subprocess.run(["git", "show-ref"], cwd=remote, capture_output=True,
                          text=True).stdout == before


# --- identity excuse: --allow-identity-hit, header lines equal to the identity only ---

HANDLE = "zorbhandle"
NOREPLY = f"12345678+{HANDLE}@users.noreply.github.com"


def _identity_push(tmp_path, monkeypatch, **over):
    """A public repo published (with --tag) under an identity the denylist hits."""
    root, remote, gh = scratch_private(tmp_path, monkeypatch, deny=("secretname", HANDLE))
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_NAME", HANDLE)
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_EMAIL", NOREPLY)
    assert gated_push(root, gh, allow_identity_hit=True, tag="v0.1.0", **over) == 0
    return root, remote


def _forge_as(tmp_path, remote, name, email, message, files=None):
    n = len(list(tmp_path.glob("idforger*")))
    work = tmp_path / f"idforger{n}"
    git(tmp_path, "clone", "-q", str(remote), str(work))
    for rel, text in (files or {f"src/z{n}.py": "z = 1\n"}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    git(work, "add", "-A")
    git(work, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-qm", message)
    git(work, "push", "-q", "origin", "main")


def test_identity_hit_refused_without_the_flag(tmp_path, monkeypatch, capsys):
    root, remote = _identity_push(tmp_path, monkeypatch)
    rc, out = _accept(root, remote, capsys)
    assert rc == 1 and "commit object" in out and "tag object" in out


def test_identity_hit_excused_on_header_lines_with_the_flag(tmp_path, monkeypatch, capsys):
    root, remote = _identity_push(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = cli.accept(root, remote=str(remote), allow_identity_hit=True)
    out = capsys.readouterr().out
    # author + committer of the sync commit, tagger of the v0.1.0 tag object
    assert rc == 0 and "scanned clean" in out and "3 denylist hit(s) excused" in out


def test_identity_excuse_never_covers_a_message_body(tmp_path, monkeypatch, capsys):
    root, remote = _identity_push(tmp_path, monkeypatch)
    _forge_as(tmp_path, remote, HANDLE, NOREPLY,
              "sync: 2026-01-02 (upstream 0000000)\n\nsigned off by " + HANDLE)
    capsys.readouterr()
    rc = cli.accept(root, remote=str(remote), allow_identity_hit=True)
    out = capsys.readouterr().out
    assert rc == 1 and "signed off by" in out


def test_identity_excuse_needs_name_and_email_to_match_exactly(tmp_path, monkeypatch, capsys):
    root, remote = _identity_push(tmp_path, monkeypatch)
    _forge_as(tmp_path, remote, HANDLE, "999+other@users.noreply.github.com",
              "sync: 2026-01-02 (upstream 0000000)")
    _forge_as(tmp_path, remote, HANDLE + " Jr", NOREPLY, "sync: 2026-01-03 (upstream 0000000)")
    capsys.readouterr()
    rc = cli.accept(root, remote=str(remote), allow_identity_hit=True)
    out = capsys.readouterr().out
    assert rc == 1 and "999+other" in out and f"{HANDLE} Jr" in out


def test_identity_excuse_never_covers_file_content(tmp_path, monkeypatch, capsys):
    root, remote = _identity_push(tmp_path, monkeypatch)
    _forge_as(tmp_path, remote, HANDLE, NOREPLY, "sync: 2026-01-02 (upstream 0000000)",
              files={"src/who.py": f"owner = '{HANDLE}'\n"})
    capsys.readouterr()
    rc = cli.accept(root, remote=str(remote), allow_identity_hit=True)
    out = capsys.readouterr().out
    assert rc == 1 and "src/who.py" in out


def test_identity_excuse_ignores_the_flag_for_a_different_identity(tmp_path, monkeypatch,
                                                                   capsys):
    """The excuse is bound to KB_PUBLISH_IDENTITY_*, not to whatever hit."""
    root, remote = _identity_push(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_NAME", "ego-OS mirror")
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_EMAIL", "mirror@invalid")
    capsys.readouterr()
    rc = cli.accept(root, remote=str(remote), allow_identity_hit=True)
    assert rc == 1 and "excused" not in capsys.readouterr().out
