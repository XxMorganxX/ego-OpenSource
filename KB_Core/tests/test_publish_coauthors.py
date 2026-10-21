"""A bad pending coauthor is dropped with a WARNING, never a publish wedge.

kb-import-pr records the PR author for credit; the denylist may grow after
that, or an entry may predate the hardening. A denylisted pending coauthor used
to refuse EVERY publish ("not overridable") until someone hand-edited the
JSON. Dropping a trailer cannot leak anything, so it is dropped (named in the
output by index only) and the publish goes on. The same holds for an email that
is not a reserved or GitHub noreply address: structural, not just denylisted.
"""
from kb_core.publish import state
from tests.publish_fakes import git, public_main, gated_push, scratch_private

NOREPLY = "4242+pat-contrib@users.noreply.github.com"


def _pending(root, *coauthors):
    st = state.load(root)
    st["pending_coauthors"] = list(coauthors)
    state.save(root, st)


def _public_message(remote):
    return git(remote, "log", "-1", "--format=%B", "main")


def test_denylisted_coauthor_dropped_and_publish_proceeds(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _pending(root, {"name": "secretname Fan", "email": NOREPLY},
             {"name": "Pat Contributor", "email": NOREPLY})
    assert gated_push(root, gh) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "coauthor[0]" in out
    msg = _public_message(remote)
    assert "secretname" not in msg.lower()
    assert f"Co-authored-by: Pat Contributor <{NOREPLY}>" in msg
    assert state.load(root)["pending_coauthors"] == []


def test_coauthor_with_a_real_mail_domain_dropped(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _pending(root, {"name": "Pat", "email": "pat" + "@" + "realmail.com"})
    assert gated_push(root, gh) == 0
    assert "WARNING" in capsys.readouterr().out
    assert "realmail" not in _public_message(remote)
    assert public_main(remote)


def test_message_and_tag_hits_still_refuse(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh, tag="secretname-1") == 1
    assert "REFUSED" in capsys.readouterr().out and public_main(remote) is None


# --- imported PRs: the push prints the `gh pr close` lines (never runs them) ---------

def test_push_prints_a_close_line_per_imported_pr(tmp_path, monkeypatch, capsys):
    from tests.publish_fakes import PUBLIC
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _pending(root, {"name": "Pat Contributor", "email": NOREPLY, "prs": [12, 3]},
             {"name": "secretname Fan", "email": NOREPLY + "x", "prs": [5, "7; rm -rf"]})
    assert gated_push(root, gh, tag="v0.1.1") == 0
    out = capsys.readouterr().out
    sync = git(remote, "rev-parse", "main")[:12]
    lines = [ln.strip() for ln in out.splitlines() if "gh pr close" in ln]
    # every shipped PR, including a dropped coauthor's; junk entries ignored
    assert lines == [f'gh pr close {n} -R {PUBLIC} --comment "Shipped in {sync} (v0.1.1). '
                     f'Thank you!"' for n in (3, 5, 12)]
    assert state.load(root)["pending_coauthors"] == []


def test_no_close_lines_without_imported_prs(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    _pending(root, {"name": "Pat Contributor", "email": NOREPLY})
    assert gated_push(root, gh) == 0
    assert "gh pr close" not in capsys.readouterr().out
