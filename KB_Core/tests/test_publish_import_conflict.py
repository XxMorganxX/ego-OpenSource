"""A PR that conflicts with UNPUBLISHED private changes gets an honest message.

"ask the contributor to rebase" was printed when the conflict came from private
changes not yet on public main — the contributor's base IS public main, so a
rebase changes nothing until the operator publishes. kb-import-pr now checks
whether origin/main changed the conflicting paths since the last publish and
says what will actually work.
"""
from kb_core.publish import import_pr, state
from tests import import_pr_fakes as ipf


def _published_then_private_edit(tmp_path):
    root = ipf.private_repo(tmp_path)
    st = state.load(root)
    st["last_published_ref"] = ipf.git(root, "rev-parse", "HEAD").strip()
    state.save(root, st)
    patch = ipf.contributor_patch(root, ipf.write("src/b.py", "b = 'contrib'\n"))
    (root / "src" / "b.py").write_text("b = 'private, unpublished'\n")
    ipf.git(root, "commit", "-qam", "private edit")
    ipf.git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root, patch


def test_conflict_with_unpublished_private_change_says_publish_first(tmp_path, capsys):
    root, patch = _published_then_private_edit(tmp_path)
    assert import_pr.run(root, 7, seams=ipf.seams(patch)) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "src/b.py" in out and "publish first" in out
    assert "ask the contributor to rebase)" not in out


def test_conflict_with_published_code_still_asks_for_a_rebase(tmp_path, capsys):
    root, patch = _published_then_private_edit(tmp_path)
    st = state.load(root)
    st["last_published_ref"] = ipf.git(root, "rev-parse", "HEAD").strip()   # now public
    state.save(root, st)
    assert import_pr.run(root, 7, seams=ipf.seams(patch)) == 1
    out = capsys.readouterr().out
    assert "rebase" in out and "publish first" not in out
