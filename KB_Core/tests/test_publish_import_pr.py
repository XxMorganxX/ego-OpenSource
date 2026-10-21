"""kb-import-pr end to end: checks, new worktree, credit.

Every test runs against a scratch private repo whose origin/main is faked
(no network); the PR patch and author come from injected seams.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from kb_core.publish import github, import_pr, manifest, state
from tests.import_pr_fakes import (AUTHOR, AUTHOR_EMAIL, PACKAGE_JSON, PYPROJECT,
                                   contributor_patch, git, private_repo, seams, write)
from tests.publish_fakes import PRIVATE, PUBLIC

PR = 7
BRANCH = f"import/pr-{PR}"


def _wt(root: Path) -> Path:
    return root / ".claude" / "worktrees" / f"import-pr-{PR}"


def _branch_exists(root: Path, name: str = BRANCH) -> bool:
    return subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/heads/{name}"],
                          cwd=root, capture_output=True).returncode == 0


def _run(root, patch, **kw):
    s = kw.pop("seams", None) or seams(patch, **{k: kw.pop(k) for k in ("author", "calls")
                                                 if k in kw})
    return import_pr.run(root, PR, seams=s, **kw)


def _untouched(root: Path, head: str) -> None:
    assert git(root, "rev-parse", "HEAD").strip() == head
    assert git(root, "branch", "--show-current").strip() == "main"
    assert git(root, "status", "--porcelain", "--untracked-files=no").strip() == ""


def _gone(root: Path) -> None:
    assert not _wt(root).exists()
    assert not _branch_exists(root)
    assert f"import-pr-{PR}" not in git(root, "worktree", "list")


# --- the happy path ---------------------------------------------------------------

def test_applies_onto_a_new_branch_in_a_new_worktree(tmp_path, capsys):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    patch = contributor_patch(root, write("src/a.py", "a = 2\n"))
    assert _run(root, patch) == 0
    out = capsys.readouterr().out
    wt = _wt(root)
    assert (wt / "src/a.py").read_text() == "a = 2\n"
    assert git(wt, "branch", "--show-current").strip() == BRANCH
    assert git(wt, "rev-parse", "HEAD~1").strip() == head          # based on origin/main
    assert git(wt, "log", "-1", "--format=%an <%ae>").strip() == "Pat Contributor <pat@example.com>"
    _untouched(root, head)
    assert (root / "src/a.py").read_text() == "a = 1\n"
    assert str(wt) in out and "review" in out.lower() and "tests" in out.lower()


def test_base_is_origin_main_not_the_checked_out_branch(tmp_path):
    root = private_repo(tmp_path)
    base = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-q", "-b", "wip")
    (root / "src/b.py").write_text("b = 99\n")
    git(root, "commit", "-qam", "wip")
    patch = contributor_patch(root, write("docs/guide.md", "# Guide\n\nMore.\n"))
    assert _run(root, patch) == 0
    assert git(_wt(root), "rev-parse", "HEAD~1").strip() == base
    assert (_wt(root) / "src/b.py").read_text() == "b = 1\n"
    assert git(root, "branch", "--show-current").strip() == "wip"


def test_fetches_origin_first_and_uses_the_pinned_public_repo(tmp_path):
    root = private_repo(tmp_path)
    calls = []
    assert _run(root, contributor_patch(root, write("src/a.py", "a = 2\n")), calls=calls) == 0
    assert calls[0][0] == "fetch"
    assert ("patch", PUBLIC, PR) in calls and ("author", PUBLIC, PR) in calls


def test_multi_commit_patch(tmp_path):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("src/a.py", "a = 2\n"), write("src/b.py", "b = 2\n"))
    assert _run(root, patch) == 0
    assert git(_wt(root), "rev-list", "--count", "HEAD~2..HEAD").strip() == "2"


def test_no_hook_runs_during_import(tmp_path):
    root = private_repo(tmp_path)
    marker = tmp_path / "hook-ran"
    hooks = root / ".git" / "hooks"
    for name in ("post-checkout", "applypatch-msg", "pre-applypatch", "post-applypatch",
                 "post-commit", "reference-transaction"):
        (hooks / name).write_text(f"#!/bin/sh\necho {name} >> '{marker}'\n")
        os.chmod(hooks / name, 0o755)
    assert _run(root, contributor_patch(root, write("src/a.py", "a = 2\n"))) == 0
    assert not marker.exists()


# --- credit ----------------------------------------------------------------------

def test_credit_comes_from_github_not_the_from_header(tmp_path, capsys):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("src/a.py", "a = 2\n"),
                              name="Forged Name", email="forged@example.com")
    assert _run(root, patch) == 0
    pending = state.load(root)["pending_coauthors"]
    assert [(c["name"], c["email"]) for c in pending] == [("Pat Contributor", AUTHOR_EMAIL)]
    assert "forged@example.com" not in json.dumps(pending)


def test_author_without_a_display_name_is_credited_by_login(tmp_path):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("src/a.py", "a = 2\n"))
    assert _run(root, patch, author=AUTHOR._replace(name="")) == 0
    assert state.load(root)["pending_coauthors"][0]["name"] == "pat-contrib"


def test_denylisted_author_is_dropped_with_a_warning_not_wedged(tmp_path, capsys):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("src/a.py", "a = 2\n"))
    assert _run(root, patch, author=AUTHOR._replace(name="Zorblax Q")) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "denylist" in out
    assert state.load(root)["pending_coauthors"] == []
    assert (_wt(root) / "src/a.py").read_text() == "a = 2\n"


def test_author_lookup_failure_warns_and_still_imports(tmp_path, capsys):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("src/a.py", "a = 2\n"))
    assert _run(root, patch, author=github.GitHubError("HTTP 502")) == 0
    assert "NOT be credited" in capsys.readouterr().out
    assert state.load(root)["pending_coauthors"] == []


@pytest.mark.parametrize("bad", [AUTHOR._replace(login="app/dependabot"),
                                 AUTHOR._replace(login="-x"),
                                 AUTHOR._replace(user_id=0)])
def test_unusable_author_is_not_credited(tmp_path, capsys, bad):
    root = private_repo(tmp_path)
    assert _run(root, contributor_patch(root, write("src/a.py", "a = 2\n")), author=bad) == 0
    assert state.load(root)["pending_coauthors"] == []
    assert "WARNING" in capsys.readouterr().out


def test_coauthor_name_is_one_plain_line():
    c = import_pr.coauthor(AUTHOR._replace(name="Pat <evil>\nSigned-off-by: x"))
    assert c["email"] == AUTHOR_EMAIL
    assert "\n" not in c["name"] and "<" not in c["name"]


def test_pending_coauthors_deduped(tmp_path):
    root = private_repo(tmp_path)
    import_pr.record_authors(root, [{"name": "Pat", "email": AUTHOR_EMAIL}])
    import_pr.record_authors(root, [{"name": "Pat C.", "email": AUTHOR_EMAIL}])
    assert len(state.load(root)["pending_coauthors"]) == 1


def test_credit_records_the_pr_number(tmp_path):
    root = private_repo(tmp_path)
    assert _run(root, contributor_patch(root, write("src/a.py", "a = 2\n"))) == 0
    assert state.load(root)["pending_coauthors"][0]["prs"] == [PR]


def test_one_author_several_prs_one_entry(tmp_path):
    root = private_repo(tmp_path)
    import_pr.record_authors(root, [{"name": "Pat", "email": AUTHOR_EMAIL}], pr=3)
    import_pr.record_authors(root, [{"name": "Pat", "email": AUTHOR_EMAIL}], pr=9)
    import_pr.record_authors(root, [{"name": "Pat", "email": AUTHOR_EMAIL}], pr=3)
    pending = state.load(root)["pending_coauthors"]
    assert len(pending) == 1 and pending[0]["prs"] == [3, 9]


# --- refusals before anything is created ------------------------------------------

def _refused_cleanly(root, head, capsys, rc, *needles):
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "REFUSED" in out
    for n in needles:
        assert n in out, (n, out)
    _gone(root)
    _untouched(root, head)
    return out


def test_manifest_widening_plus_private_path_is_refused(tmp_path, capsys):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    widened = (root / "publish.manifest.toml").read_text().replace(
        '"src/**",', '"src/**", "private/**",')
    patch = contributor_patch(root, write("publish.manifest.toml", widened),
                              write("private/leak.md", "x\n"))
    _refused_cleanly(root, head, capsys, _run(root, patch), "private/leak.md",
                     "not exported", "publish.manifest.toml")


@pytest.mark.parametrize("rel", ["private/new.md", "src/internal/y.py", "brand-new/x.py"])
def test_unselected_paths_are_refused(tmp_path, capsys, rel):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    _refused_cleanly(root, head, capsys, _run(root, contributor_patch(root, write(rel, "x\n"))),
                     rel)


@pytest.mark.parametrize("rel", [".github/workflows/ci.yml", "conftest.py", "scripts/x.sh",
                                 "KB_Core/src/kb_core/publish/check.py", "uv.lock"])
def test_control_files_are_refused_without_the_flag(tmp_path, capsys, rel):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    out = _refused_cleanly(root, head, capsys,
                           _run(root, contributor_patch(root, write(rel, "x = 1\n"))),
                           rel, "--allow-control-files")


def test_symlink_is_refused_even_with_allow_control_files(tmp_path, capsys):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()

    def _ln(clone):
        os.symlink("../private/notes.md", clone / "src/link.md")
    rc = _run(root, contributor_patch(root, _ln), allow_control_files=True, yes=True)
    _refused_cleanly(root, head, capsys, rc, "symlink")


def test_deny_hit_in_added_content_is_refused(tmp_path, capsys):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    patch = contributor_patch(root, write("docs/guide.md", "thanks Zorblax\n"))
    _refused_cleanly(root, head, capsys,
                     _run(root, patch, allow_control_files=True, yes=True), "denylist")


def test_deny_hit_only_in_the_commit_message_and_from_header_is_not_a_refusal(tmp_path):
    # Private commit metadata never reaches the public repo (fresh-history sync
    # commits); refusing on it would wedge any contributor who shares a name.
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("src/a.py", "a = 2\n"), name="Zorblax Fan",
                              message="zorblax: fix")
    assert _run(root, patch) == 0


def test_unparseable_patch_is_refused(tmp_path, capsys):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    patch = contributor_patch(root, write("src/a.py", "a = 2\n")).replace(
        "diff --git a/src/a.py b/src/a.py", 'diff --git "a/src/a.py" "b/src/a.py"')
    _refused_cleanly(root, head, capsys, _run(root, patch), "quoted")


def test_empty_patch_is_refused(tmp_path, capsys):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    _refused_cleanly(root, head, capsys, _run(root, "Subject: nothing\n"), "no file")


def test_no_source_repo_refuses_before_any_seam(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("KB_PUBLISH_REMOTE", raising=False)
    root = private_repo(tmp_path, pin=False)
    calls = []
    assert _run(root, "x", calls=calls) == 1
    assert calls == []
    out = capsys.readouterr().out
    assert "REFUSED" in out and "fork-facing placeholder" in out


def test_env_remote_must_match_the_pin(tmp_path, capsys, monkeypatch):
    root = private_repo(tmp_path)
    monkeypatch.setenv("KB_PUBLISH_REMOTE", "https://github.com/someone/else.git")
    calls = []
    assert _run(root, "x", calls=calls) == 1 and calls == []
    assert "pinned" in capsys.readouterr().out


def test_env_remote_alone_is_accepted(tmp_path, monkeypatch):
    root = private_repo(tmp_path, pin=False)
    monkeypatch.setenv("KB_PUBLISH_REMOTE", f"ssh://git@github.com/{PUBLIC}.git")
    calls = []
    assert _run(root, contributor_patch(root, write("src/a.py", "a = 2\n")), calls=calls) == 0
    assert ("patch", PUBLIC, PR) in calls


def test_source_repo_that_is_the_private_origin_is_refused(tmp_path, capsys, monkeypatch):
    root = private_repo(tmp_path, pin=False)
    monkeypatch.setenv("KB_PUBLISH_REMOTE", f"https://github.com/{PRIVATE}.git")
    assert _run(root, "x", calls=[]) == 1
    assert "private origin" in capsys.readouterr().out


def test_non_github_source_is_refused(tmp_path, capsys, monkeypatch):
    root = private_repo(tmp_path, pin=False)
    monkeypatch.setenv("KB_PUBLISH_REMOTE", "/some/local/path.git")
    assert _run(root, "x", calls=[]) == 1
    assert "github.com" in capsys.readouterr().out


def test_missing_deny_file_at_origin_main_is_refused(tmp_path, capsys):
    root = private_repo(tmp_path, deny=False)
    calls = []
    assert _run(root, "x", calls=calls) == 1
    assert "publish.deny.toml" in capsys.readouterr().out
    assert not any(c[0] == "patch" for c in calls)


@pytest.mark.parametrize("deny_text", ['[deny]\npatterns = []\n', "[deny]\n", "not toml ["])
def test_empty_or_broken_deny_file_at_origin_main_is_refused(tmp_path, capsys, deny_text):
    root = private_repo(tmp_path)
    (root / "publish.deny.toml").write_text(deny_text)
    git(root, "commit", "-qam", "weaken")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    calls = []
    assert _run(root, "x", calls=calls) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert not any(c[0] == "patch" for c in calls)


def test_user_diff_and_color_config_cannot_confuse_the_backstop(tmp_path):
    root = private_repo(tmp_path)
    for k, v in (("color.ui", "always"), ("color.diff", "always"), ("diff.noprefix", "true"),
                 ("diff.mnemonicPrefix", "true"), ("diff.renames", "copies"),
                 ("core.quotePath", "false")):
        git(root, "config", k, v)
    assert _run(root, contributor_patch(root, write("docs/new guide.md", "x\n"))) == 0


def test_deny_file_is_read_at_origin_main_not_the_working_tree(tmp_path, capsys):
    root = private_repo(tmp_path)
    (root / "publish.deny.toml").write_text('[deny]\npatterns = ["nothingmatches"]\n')
    patch = contributor_patch(root, write("docs/guide.md", "thanks Zorblax\n"))
    assert _run(root, patch) == 1
    assert "denylist" in capsys.readouterr().out


def test_failed_origin_fetch_is_refused(tmp_path, capsys):
    from kb_core.publish import gate
    root = private_repo(tmp_path)

    def _boom(root):
        raise gate.GateError("git fetch origin failed: offline")
    s = seams("x", fetch_origin=_boom)
    assert import_pr.run(root, PR, seams=s) == 1
    assert "fetch" in capsys.readouterr().out


def test_existing_branch_or_worktree_is_refused(tmp_path, capsys):
    root = private_repo(tmp_path)
    git(root, "branch", BRANCH)
    patch = contributor_patch(root, write("src/a.py", "a = 2\n"))
    assert _run(root, patch) == 1
    assert "already exists" in capsys.readouterr().out
    git(root, "branch", "-D", BRANCH)
    _wt(root).mkdir(parents=True)
    assert _run(root, patch) == 1
    assert "already exists" in capsys.readouterr().out


@pytest.mark.parametrize("n", [0, -3])
def test_bad_pr_number_is_refused(tmp_path, capsys, n):
    root = private_repo(tmp_path)
    assert import_pr.run(root, n, seams=seams("x", calls=[])) == 1


# --- refusals after apply: the worktree and branch are removed ---------------------

def test_patch_that_does_not_apply_is_refused_and_cleaned_up(tmp_path, capsys):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("src/a.py", "a = 2\n"))
    (root / "src/a.py").write_text("a = 999\n")
    git(root, "commit", "-qam", "diverge")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    head = git(root, "rev-parse", "HEAD").strip()
    _refused_cleanly(root, head, capsys, _run(root, patch), "did not apply")


@pytest.mark.parametrize("edit,needle", [
    (lambda t: t.replace('"requests>=2"', '"requests>=2", "evil-pkg"'), "dependencies"),
    (lambda t: t.replace('demo = "demo:main"', 'demo = "evil:main"'), "scripts"),
])
def test_pyproject_dependency_or_script_change_is_control(tmp_path, capsys, edit, needle):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    patch = contributor_patch(root, write("pyproject.toml", edit(PYPROJECT)))
    _refused_cleanly(root, head, capsys, _run(root, patch), "pyproject.toml", needle,
                     "--allow-control-files")


def test_package_json_scripts_change_is_control(tmp_path, capsys):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    d = json.loads(PACKAGE_JSON)
    d["scripts"]["postinstall"] = "node steal.js"
    patch = contributor_patch(root, write("package.json", json.dumps(d, indent=2) + "\n"))
    _refused_cleanly(root, head, capsys, _run(root, patch), "package.json", "scripts")


def test_descriptive_pyproject_change_is_not_control(tmp_path):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("pyproject.toml", PYPROJECT.replace(
        'description = "demo package"', 'description = "a better demo"')))
    assert _run(root, patch) == 0


# --- --allow-control-files ---------------------------------------------------------

def test_allow_control_files_prints_full_diff_and_still_needs_yes(tmp_path, capsys):
    root = private_repo(tmp_path)
    head = git(root, "rev-parse", "HEAD").strip()
    body = "".join(f"line {i}\n" for i in range(60))
    patch = contributor_patch(root, write("scripts/build.sh", body))
    # No --yes and no terminal (pytest's stdin): print everything, then refuse.
    rc = _run(root, patch, allow_control_files=True, seams=seams(patch, confirm=None))
    _refused_cleanly(root, head, capsys, rc, "scripts/build.sh", "+line 0", "+line 59", "--yes")


def test_allow_control_files_with_yes_imports(tmp_path, capsys):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("scripts/build.sh", "echo hi\n"))
    assert _run(root, patch, allow_control_files=True, yes=True) == 0
    out = capsys.readouterr().out
    assert "+echo hi" in out
    assert (_wt(root) / "scripts/build.sh").read_text() == "echo hi\n"


def test_allow_control_files_interactive_confirm(tmp_path, capsys):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("conftest.py", "x = 1\n"))
    asked = []
    s = seams(patch, confirm=lambda prompt: asked.append(prompt) or True)
    assert import_pr.run(root, PR, seams=s, allow_control_files=True) == 0
    assert asked
    head = git(root, "rev-parse", "HEAD").strip()
    git(root, "worktree", "remove", "--force", str(_wt(root)))
    git(root, "branch", "-D", BRANCH)
    s = seams(patch, confirm=lambda prompt: False)
    _refused_cleanly(root, head, capsys,
                     import_pr.run(root, PR, seams=s, allow_control_files=True), "not confirmed")


def test_control_diff_neutralizes_terminal_escapes(tmp_path, capsys):
    root = private_repo(tmp_path)
    patch = contributor_patch(root, write("scripts/x.sh", "echo \x1b[2Jcleared\n"))
    assert _run(root, patch, allow_control_files=True, yes=True) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\\x1b[2J" in out


# --- the post-apply backstop -------------------------------------------------------

def _m(root):
    return manifest.load_manifest(root / "publish.manifest.toml")


def test_verify_applied_refuses_paths_the_parse_did_not_see(tmp_path):
    root = private_repo(tmp_path)
    base = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-q", "-b", "applied")
    (root / "src/a.py").write_text("a = 2\n")
    (root / "src/b.py").write_text("b = 2\n")
    git(root, "commit", "-qam", "two files")
    seen = [import_pr.FileChange("modify", "src/a.py", "src/a.py", "100644", "100644",
                                 False, (), ("a = 2",))]
    with pytest.raises(import_pr.ImportRefused, match="src/b.py"):
        import_pr.verify_applied(root, base, seen, _m(root), [re.compile("zorblax", re.I)])


def test_verify_applied_rechecks_policy_on_what_git_wrote(tmp_path):
    root = private_repo(tmp_path)
    base = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-q", "-b", "applied")
    (root / "private/notes.md").write_text("changed\n")
    git(root, "commit", "-qam", "private edit")
    seen = [import_pr.FileChange("modify", "private/notes.md", "private/notes.md", "100644",
                                 "100644", False, (), ("changed",))]
    with pytest.raises(import_pr.ImportRefused, match="not exported"):
        import_pr.verify_applied(root, base, seen, _m(root), [re.compile("zorblax", re.I)])


def test_verify_applied_refuses_merge_commits(tmp_path):
    root = private_repo(tmp_path)
    base = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-q", "-b", "side")
    (root / "src/a.py").write_text("a = 2\n")
    git(root, "commit", "-qam", "side")
    git(root, "checkout", "-q", "-b", "applied", base)
    (root / "src/b.py").write_text("b = 2\n")
    git(root, "commit", "-qam", "main")
    git(root, "merge", "-q", "--no-edit", "side")
    changes = import_pr.parse_patch(git(root, "diff", "--no-renames", base, "HEAD"))
    with pytest.raises(import_pr.ImportRefused, match="merge"):
        import_pr.verify_applied(root, base, changes, _m(root), [re.compile("zorblax", re.I)])


# --- default gh seams: argv and parsing only (fake runner) ------------------------

class _Run:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.cmds = []

    def __call__(self, cmd, **kw):
        self.cmds.append((cmd, kw))
        rc, out = self.outputs.pop(0)
        return subprocess.CompletedProcess(cmd, rc, out, "boom" if rc else "")


def test_gh_pr_patch_argv_and_decoding():
    run = _Run((0, b"From x\n"))
    assert import_pr.gh_pr_patch("acme/mirror", 7, run=run) == "From x\n"
    cmd, kw = run.cmds[0]
    assert cmd == ["gh", "pr", "diff", "7", "--patch", "--repo", "acme/mirror"]
    assert kw["env"]["GH_HOST"] == "github.com" and kw["timeout"]
    with pytest.raises(import_pr.ImportRefused, match="UTF-8"):
        import_pr.gh_pr_patch("acme/mirror", 7, run=_Run((0, b"\xff\xfe")))
    with pytest.raises(github.GitHubError):
        import_pr.gh_pr_patch("acme/mirror", 7, run=_Run((1, b"")))


def test_gh_pr_author_reads_pr_author_then_numeric_user_id():
    pr = json.dumps({"author": {"login": "pat-contrib", "name": "Pat C", "is_bot": False,
                                "id": "MDQ6VXNlcjQyNDI="}})
    run = _Run((0, pr), (0, json.dumps({"id": 4242, "login": "pat-contrib"})))
    assert import_pr.gh_pr_author("acme/mirror", 7, run=run) == import_pr.PrAuthor(
        "pat-contrib", "Pat C", 4242)
    assert run.cmds[0][0] == ["gh", "pr", "view", "7", "--repo", "acme/mirror",
                              "--json", "author"]
    assert run.cmds[1][0] == ["gh", "api", "users/pat-contrib"]


@pytest.mark.parametrize("outputs", [
    [(1, "")],
    [(0, "not json")],
    [(0, json.dumps({"author": {"login": "x", "is_bot": True}}))],
    [(0, json.dumps({"author": {"login": "pat"}})), (0, json.dumps({"id": "nope"}))],
    [(0, json.dumps({"author": {"login": "pat"}})), (0, json.dumps({"id": 5, "login": "other"}))],
])
def test_gh_pr_author_fails_closed(outputs):
    with pytest.raises(github.GitHubError):
        import_pr.gh_pr_author("acme/mirror", 7, run=_Run(*outputs))


def test_main_parses_flags(monkeypatch, tmp_path):
    got = {}

    def _fake_run(root, pr, **kw):
        got.update(pr=pr, **kw)
        return 0
    monkeypatch.setattr(import_pr, "run", _fake_run)
    monkeypatch.setattr(import_pr, "_toplevel", lambda: tmp_path)
    assert import_pr.main(["12", "--allow-control-files", "--yes"]) == 0
    assert got == {"pr": 12, "allow_control_files": True, "yes": True}
