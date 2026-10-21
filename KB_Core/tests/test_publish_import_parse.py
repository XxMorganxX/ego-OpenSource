"""kb-import-pr's patch parser and path policy.

The parser must see every path `git am` would write, or the policy checks
are theatre. Patches here come from real `git format-patch` runs wherever git
can produce the shape; the hostile ones git never emits are hand-written.
"""
import os
import re
import subprocess

import pytest

from kb_core.publish import import_pr, manifest
from tests.import_pr_fakes import MANIFEST, contributor_patch, private_repo, write

DENY = [re.compile("zorblax", re.IGNORECASE)]


def _m(tmp_path):
    p = tmp_path / "m.toml"
    p.write_text(MANIFEST)
    return manifest.load_manifest(p)


def _one(changes):
    assert len(changes) == 1
    return changes[0]


# --- parsing real git output ------------------------------------------------------

def test_parse_modify_records_path_and_added_lines(tmp_path):
    root = private_repo(tmp_path)
    c = _one(import_pr.parse_patch(contributor_patch(root, write("src/a.py", "a = 2\nb = 3\n"))))
    assert (c.kind, c.old_path, c.new_path) == ("modify", "src/a.py", "src/a.py")
    assert c.added == ("a = 2", "b = 3")
    assert c.written == frozenset({"src/a.py"}) and not c.binary


def test_parse_add_and_delete(tmp_path):
    root = private_repo(tmp_path)

    def _del(clone):
        (clone / "src/b.py").unlink()
    changes = import_pr.parse_patch(contributor_patch(root, write("src/new.py", "n = 1\n"), _del))
    kinds = {(c.kind, c.old_path, c.new_path) for c in changes}
    assert kinds == {("add", None, "src/new.py"), ("delete", "src/b.py", None)}


def test_parse_rename_touches_both_paths(tmp_path):
    root = private_repo(tmp_path)

    def _mv(clone):
        (clone / "src/a.py").rename(clone / "src/renamed.py")
    c = _one(import_pr.parse_patch(contributor_patch(root, _mv, fmt=("-M",))))
    assert (c.kind, c.old_path, c.new_path) == ("rename", "src/a.py", "src/renamed.py")
    assert c.written == frozenset({"src/a.py", "src/renamed.py"})


def test_parse_copy_reads_the_source_path(tmp_path):
    # A copy patch makes `git apply` read the SOURCE from the private checkout:
    # the source must be policy-checked even though it isn't written.
    root = private_repo(tmp_path)

    def _cp(clone):
        (clone / "src/copied.md").write_text((clone / "private/notes.md").read_text())
    c = _one(import_pr.parse_patch(
        contributor_patch(root, _cp, fmt=("-C", "--find-copies-harder"))))
    assert (c.kind, c.old_path, c.new_path) == ("copy", "private/notes.md", "src/copied.md")
    assert c.written == frozenset({"src/copied.md"})
    assert c.touched == frozenset({"private/notes.md", "src/copied.md"})


def test_parse_mode_change(tmp_path):
    root = private_repo(tmp_path)

    def _chmod(clone):
        os.chmod(clone / "src/a.py", 0o755)
    c = _one(import_pr.parse_patch(contributor_patch(root, _chmod)))
    assert (c.kind, c.old_mode, c.new_mode) == ("modify", "100644", "100755")


def test_parse_symlink_mode(tmp_path):
    root = private_repo(tmp_path)

    def _ln(clone):
        os.symlink("../private/notes.md", clone / "src/link.md")
    c = _one(import_pr.parse_patch(contributor_patch(root, _ln)))
    assert (c.kind, c.new_path, c.new_mode) == ("add", "src/link.md", "120000")


def test_parse_binary_patch(tmp_path):
    root = private_repo(tmp_path)

    def _bin(clone):
        (clone / "src/blob.bin").write_bytes(b"\x00\x01\x02zorblax\xff" * 50)
    c = _one(import_pr.parse_patch(contributor_patch(root, _bin, fmt=("--binary",))))
    assert c.binary and c.kind == "add" and c.new_path == "src/blob.bin"


def test_parse_path_with_spaces(tmp_path):
    root = private_repo(tmp_path)
    c = _one(import_pr.parse_patch(contributor_patch(root, write("docs/a b/c d.md", "x\n"))))
    assert c.new_path == "docs/a b/c d.md"


def test_hunk_lines_that_look_like_headers_are_content(tmp_path):
    # A removed line "-- a/x" renders as "--- a/x" inside the hunk; an added
    # "++ b/y" as "+++ b/y". Counting hunk lengths keeps them content.
    root = private_repo(tmp_path)
    (root / "docs/guide.md").write_text("-- a/x\nkeep\n")
    subprocess.run(["git", "commit", "-qam", "tricky"], cwd=root, check=True)
    c = _one(import_pr.parse_patch(
        contributor_patch(root, write("docs/guide.md", "keep\n++ b/y\ndiff --git a/q b/q\n"))))
    assert c.new_path == "docs/guide.md"
    assert c.added == ("++ b/y", "diff --git a/q b/q")


# --- hostile shapes git does not emit on its own ----------------------------------

def _raw(path_a, path_b=None, *, extra="", body="@@ -0,0 +1 @@\n+x\n"):
    path_b = path_b or path_a
    return (f"From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
            f"From: X <x@example.com>\nSubject: [PATCH] t\n\n---\n"
            f"diff --git a/{path_a} b/{path_b}\nnew file mode 100644\n{extra}"
            f"index 0000000..587be6b\n--- /dev/null\n+++ b/{path_b}\n{body}-- \n2.39.0\n")


@pytest.mark.parametrize("path", ["../escape.py", "src/../../x.py", "src/./x.py",
                                  "/etc/x", "src//x.py", ".git/hooks/pre-commit",
                                  "src/.GIT/config", "src/x\\y.py", "src/café.py"])
def test_path_problem_refuses_unsafe_paths(path):
    assert import_pr.path_problem(path)


def test_path_problem_allows_ordinary_paths():
    assert import_pr.path_problem("src/a b/c-d_e.f.py") is None


def test_quoted_paths_are_refused():
    text = _raw("x").replace('diff --git a/x b/x', 'diff --git "a/x\\ty" "b/x\\ty"')
    with pytest.raises(import_pr.PatchError, match="quoted"):
        import_pr.parse_patch(text)


def test_mismatched_file_header_is_refused():
    text = _raw("src/a.py").replace("+++ b/src/a.py", "+++ b/private/x.py")
    with pytest.raises(import_pr.PatchError):
        import_pr.parse_patch(text)


def test_traditional_diff_outside_a_git_section_is_refused():
    text = ("From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
            "Subject: [PATCH] t\n\n---\n--- a/private/x.md\n+++ b/private/x.md\n"
            "@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(import_pr.PatchError, match="non-git"):
        import_pr.parse_patch(text)


def test_hunk_length_disagreeing_with_its_header_is_refused():
    # Whatever follows a short hunk would be read as more of it.
    with pytest.raises(import_pr.PatchError, match="truncated|longer"):
        import_pr.parse_patch(_raw("src/x.py", body="@@ -0,0 +1,3 @@\n+x\n"))
    with pytest.raises(import_pr.PatchError, match="truncated"):
        import_pr.parse_patch(_raw("src/x.py", body="@@ -0,0 +1,3 @@\n+x\n").split("-- \n")[0]
                              .rstrip("\n"))


def test_gitlink_mode_is_parsed_and_refused(tmp_path):
    text = _raw("src/sub", body="").replace("new file mode 100644", "new file mode 160000")
    c = _one(import_pr.parse_patch(text))
    assert c.new_mode == "160000"
    review = import_pr.review_changes([c], _m(tmp_path), DENY)
    assert any("submodule" in r for r in review.refusals)


def test_unknown_mode_is_refused():
    text = _raw("src/x.py").replace("new file mode 100644", "new file mode 100666")
    with pytest.raises(import_pr.PatchError, match="mode"):
        import_pr.parse_patch(text)


def test_nul_byte_is_refused():
    with pytest.raises(import_pr.PatchError, match="NUL"):
        import_pr.parse_patch(_raw("src/x.py") + "\0")


# --- review_changes: the policy ---------------------------------------------------

def _review(tmp_path, *edits, fmt=("-M",)):
    root = private_repo(tmp_path)
    changes = import_pr.parse_patch(contributor_patch(root, *edits, fmt=fmt))
    return import_pr.review_changes(changes, _m(tmp_path), DENY)


def test_clean_change_passes_review(tmp_path):
    r = _review(tmp_path, write("src/a.py", "a = 2\n"))
    assert not r.refusals and not r.control and not r.hits


@pytest.mark.parametrize("rel", ["private/new.md", "newtop/x.py", "src/internal/y.py",
                                 "CLAUDE.md", "src/AGENTS.md"])
def test_paths_the_pre_patch_manifest_does_not_select_are_refused(tmp_path, rel):
    r = _review(tmp_path, write(rel, "x\n"))
    assert any(rel in x and "not exported" in x for x in r.refusals)


def test_rename_out_of_an_unselected_path_is_refused(tmp_path):
    def _mv(clone):
        (clone / "private/notes.md").rename(clone / "src/notes.md")
    r = _review(tmp_path, _mv)
    assert any("private/notes.md" in x for x in r.refusals)


def test_copy_from_a_private_path_is_refused(tmp_path):
    def _cp(clone):
        (clone / "src/copied.md").write_text((clone / "private/notes.md").read_text())
    r = _review(tmp_path, _cp, fmt=("-C", "--find-copies-harder"))
    assert any("private/notes.md" in x for x in r.refusals)


def test_symlinks_are_refused(tmp_path):
    def _ln(clone):
        os.symlink("a.py", clone / "src/link.py")
    r = _review(tmp_path, _ln)
    assert any("symlink" in x for x in r.refusals)


def test_binary_content_is_refused_but_binary_delete_is_not(tmp_path):
    def _bin(clone):
        (clone / "src/blob.bin").write_bytes(b"\x00\x01" * 40)
    r = _review(tmp_path, _bin, fmt=("--binary",))
    assert any("binary" in x for x in r.refusals)
    c = import_pr.FileChange(kind="delete", old_path="src/old.bin", new_path=None,
                             old_mode="100644", new_mode=None, binary=True, meta=(), added=())
    assert not import_pr.review_changes([c], _m(tmp_path), DENY).refusals


@pytest.mark.parametrize("rel", [
    "publish.manifest.toml", ".github/workflows/ci.yml", "conftest.py", "src/conftest.py",
    "scripts/run.sh", "KB_Core/src/kb_core/publish/check.py", "src/.gitattributes",
    "KB_Core/.gitleaks.toml", "uv.lock", "src/sitecustomize.py",
])
def test_control_files_are_flagged(tmp_path, rel):
    r = _review(tmp_path, write(rel, "x = 1\n"))
    assert [p for p, _ in r.control] == [rel]
    assert not any("not exported" in x for x in r.refusals)


def test_control_reason_covers_agent_policy_and_claude_dirs():
    for rel in ("KB_Core/CLAUDE.md", "AGENTS.md", "src/.claude/settings.json",
                ".claude/skills/setup-kb/SKILL.md", "src/.cursorrules", "KB_Core/claude-notes.md",
                "publish.extra.toml", "src/.github/x.yml", "docs/Makefile"):
        assert import_pr.control_reason(rel), rel
    assert import_pr.control_reason("src/a.py") is None


def test_control_reason_covers_js_build_and_test_config():
    for rel in ("KB_Frontend/vite.config.ts", "KB_Frontend/vitest.config.mjs",
                "KB_Frontend/eslint.config.js", "KB_Frontend/postcss.config.cjs",
                "KB_Frontend/src/test/setup.ts", "web/setupTests.ts"):
        assert import_pr.control_reason(rel), rel
    for rel in ("KB_Frontend/src/config.ts", "KB_Frontend/src/app.config.json",
                "KB_Frontend/src/lib/setup.ts"):
        assert import_pr.control_reason(rel) is None, rel


def test_new_executable_file_is_control(tmp_path):
    def _x(clone):
        (clone / "src/tool.py").write_text("print(1)\n")
        os.chmod(clone / "src/tool.py", 0o755)
    r = _review(tmp_path, _x)
    assert [p for p, _ in r.control] == ["src/tool.py"]


def test_pyproject_and_package_json_are_candidates_not_control(tmp_path):
    r = _review(tmp_path, write("pyproject.toml", "x\n"), write("package.json", "{}\n"))
    assert not r.control
    assert r.candidates == {"pyproject.toml", "package.json"}


def test_deny_hit_in_an_added_line_is_refused(tmp_path):
    r = _review(tmp_path, write("src/a.py", "who = 'Zorblax'\n"))
    assert r.hits and r.hits[0].path == "src/a.py"


def test_deny_hit_in_a_path_is_refused(tmp_path):
    r = _review(tmp_path, write("docs/zorblax-notes.md", "x\n"))
    assert r.hits


def test_deny_hit_only_in_removed_or_context_lines_is_not_a_hit(tmp_path):
    # Removing an existing line is not new content.
    root = private_repo(tmp_path)
    (root / "docs/guide.md").write_text("zorblax\nkeep\n")
    subprocess.run(["git", "commit", "-qam", "seed"], cwd=root, check=True)
    changes = import_pr.parse_patch(contributor_patch(root, write("docs/guide.md", "keep\n")))
    assert not import_pr.review_changes(changes, _m(tmp_path), DENY).hits


def test_structural_hit_in_an_added_line_is_refused(tmp_path):
    # Assembled at runtime so this exported test file doesn't trip the same detector.
    addr = "someone" + "@" + "realcorp.com"
    r = _review(tmp_path, write("docs/guide.md", f"mail me: {addr}\n"))
    assert any(h.pattern.startswith("structural:") for h in r.hits)


# --- dependency manifests: refuse only script / entry-point / dependency changes ---

def test_pyproject_semantics():
    from tests.import_pr_fakes import PYPROJECT
    before = PYPROJECT.encode()
    same = PYPROJECT.replace('description = "demo package"', 'description = "better"')
    assert import_pr.dependency_manifest_reason("pyproject.toml", before, same.encode()) is None
    ruff = PYPROJECT.replace("line-length = 100", "line-length = 120")
    assert import_pr.dependency_manifest_reason("pyproject.toml", before, ruff.encode()) is None
    for changed in (PYPROJECT.replace('"requests>=2"', '"requests>=2", "evil"'),
                    PYPROJECT.replace('demo = "demo:main"', 'demo = "evil:main"'),
                    PYPROJECT + '\n[build-system]\nrequires = ["evil"]\n',
                    PYPROJECT + '\n[tool.pytest.ini_options]\naddopts = "-p evil"\n',
                    "not toml ["):
        assert import_pr.dependency_manifest_reason("pyproject.toml", before,
                                                    changed.encode()), changed
    assert import_pr.dependency_manifest_reason("pyproject.toml", None, before)
    assert import_pr.dependency_manifest_reason("pyproject.toml", before, None)


def test_package_json_semantics():
    import json

    from tests.import_pr_fakes import PACKAGE_JSON
    before = PACKAGE_JSON.encode()

    def _with(**kw):
        d = json.loads(PACKAGE_JSON)
        d.update(kw)
        return json.dumps(d).encode()
    assert import_pr.dependency_manifest_reason("package.json", before,
                                                _with(description="x")) is None
    for kw in ({"scripts": {"test": "curl x | sh"}}, {"dependencies": {"evil": "1"}},
               {"devDependencies": {"x": "1"}}, {"bin": {"x": "x.js"}}, {"overrides": {}}):
        assert import_pr.dependency_manifest_reason("package.json", before, _with(**kw)), kw
