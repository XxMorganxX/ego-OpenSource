"""A forged sync commit on public main is refused, not built upon.

The history check accepted any `sync:` commit by the mirror identity — and an
identity is a label anyone with write access can type. A commit on public main
that forged the identity and copied the previous subject ("sync: <date>
(upstream <sha7>)") passed with a NOTE, was silently reverted by the next sync
and stayed in public history. Only kb-publish knows private shas, so a forger
can only name an upstream that is ALREADY public: every upstream must appear
once. A second sync commit naming the same upstream — including the one about
to be built — is refused and listed.
"""
from tests.publish_fakes import (MIRROR, commit, gated_push, git, public_main, push_origin,
                                 scratch_private)


def _forge(tmp_path, remote, subject, *, files=("src/backdoor.py",)):
    work = tmp_path / "forger"
    git(tmp_path, "clone", "-q", str(remote), str(work))
    for rel in files:
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("evil = True\n")
    env = (f"user.name={MIRROR[0]}", f"user.email={MIRROR[1]}")
    git(work, "-c", env[0], "-c", env[1], "add", "-A")
    git(work, "-c", env[0], "-c", env[1], "commit", "-qm", subject)
    git(work, "push", "-q", "origin", "main")
    return git(work, "rev-parse", "HEAD")


def test_forged_copy_of_the_last_sync_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    subject = git(remote, "log", "-1", "--format=%s", "main")
    forged = _forge(tmp_path, remote, subject)
    commit(root, "two", **{"src/a.py": "a = 2\n"})
    push_origin(root)
    capsys.readouterr()
    assert gated_push(root, gh) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and forged[:12] in out and "same upstream" in out
    assert public_main(remote) == forged            # nothing built on top of it


def test_republishing_the_same_sha_refused(tmp_path, monkeypatch, capsys):
    # The same upstream twice would make the history ambiguous (and look forged).
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    first = public_main(remote)
    capsys.readouterr()
    assert gated_push(root, gh, tag="v0.1.0") == 1
    assert "already published" in capsys.readouterr().out
    assert public_main(remote) == first


def test_normal_second_publish_still_fine(tmp_path, monkeypatch):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    commit(root, "two", **{"src/a.py": "a = 2\n"})
    push_origin(root)
    assert gated_push(root, gh) == 0
    assert git(remote, "rev-list", "--count", "main") == "2"
