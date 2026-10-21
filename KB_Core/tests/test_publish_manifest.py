from kb_core.publish import manifest

MANIFEST_TOML = """
[publish]
remote = "https://github.com/example/pub.git"

[export]
include = ["KB_Core/**", "README.md"]
exclude = ["KB_Core/docs/plans/**", "**/CLAUDE.md"]
"""


def _m(tmp_path):
    p = tmp_path / "publish.manifest.toml"
    p.write_text(MANIFEST_TOML)
    return manifest.load_manifest(p)


def test_load(tmp_path):
    m = _m(tmp_path)
    assert m.remote == "https://github.com/example/pub.git"
    assert "KB_Core/**" in m.include


def test_select_include_exclude(tmp_path):
    m = _m(tmp_path)
    paths = [
        "KB_Core/src/kb_core/db.py",          # included (nested)
        "KB_Core/docs/plans/old-plan.md",     # excluded subdir
        "KB_Core/CLAUDE.md",                  # excluded by **/CLAUDE.md
        "README.md",                          # exact include
        "CLAUDE.md",                          # root file, never included
        "private/evals/gold.jsonl",           # not matched by any include
        "publish.deny.toml",                  # not matched by any include
    ]
    assert manifest.select(paths, m) == ["KB_Core/src/kb_core/db.py", "README.md"]


def test_new_toplevel_dir_is_private_by_default(tmp_path):
    m = _m(tmp_path)
    assert manifest.select(["newdir/anything.py"], m) == []


from pathlib import Path

REAL_MANIFEST = Path(__file__).resolve().parents[2] / "publish.manifest.toml"


def test_real_manifest_exports_setup_skill_and_nothing_else_under_dot_claude():
    """The setup guide + its skill export; every other .claude path stays private."""
    m = manifest.load_manifest(REAL_MANIFEST)
    paths = [
        ".claude/skills/setup-kb/SKILL.md",          # the one skill we publish
        "docs/setup/README.md",                      # the guide
        "docs/setup/recipes/repo-docs.md",           # a recipe
        ".claude/hooks/inference-tracking-reminder.py",  # root hook: private
        ".claude/settings.json",                     # root settings: private
        "KB_Core/.claude/settings.json",             # sub-repo .claude: private
        "KB_Core/.claude/skills/creating-kb-adapters/SKILL.md",  # sub-repo skill: private
        "KB_Frontend/.claude/skills/verify/SKILL.md",            # sub-repo skill: private
    ]
    assert manifest.select(paths, m) == [
        ".claude/skills/setup-kb/SKILL.md",
        "docs/setup/README.md",
        "docs/setup/recipes/repo-docs.md",
    ]


def test_real_manifest_exports_tenancy_code_and_runbooks_never_the_registry():
    """KB_Agent and the ops runbooks the README links export; the tenant registry
    (emails + agent secrets) and per-tenant overlay dirs never do, wherever they sit."""
    m = manifest.load_manifest(REAL_MANIFEST)
    public = [
        "KB_Agent/pyproject.toml",
        "KB_Agent/src/kb_agent/jobs.py",
        "docs/ops/tenant-onboarding.md",
        "docs/ops/backup-restore.md",
        "KB_Core/src/kb_core/tenants.py",          # the code, not the registry
        "KB_Core/inference.tenant.toml",
    ]
    private = [
        "KB_Core/tenants.toml",
        "KB_Core/tenants/ann/policy.toml",
        "KB_Core/tenants/ann/prompts/doc_kind.md",
        "KB_Agent/tenants.toml",
    ]
    assert manifest.select(public + private, m) == public


def test_unapproved_media(tmp_path):
    p = tmp_path / "publish.manifest.toml"
    p.write_text(MANIFEST_TOML.replace(
        'exclude = [', 'allow_media = ["KB_Core/docs/logo.png"]\nexclude = ['))
    m = manifest.load_manifest(p)
    paths = ["KB_Core/docs/logo.png", "KB_Core/shot.PNG", "KB_Core/a.pdf",
             "KB_Core/icon.svg", "KB_Core/x.py"]
    assert manifest.unapproved_media(paths, m) == ["KB_Core/shot.PNG", "KB_Core/a.pdf"]


def test_real_manifest_ships_no_unapproved_media():
    import subprocess
    m = manifest.load_manifest(REAL_MANIFEST)
    tracked = subprocess.run(["git", "ls-files"], cwd=REAL_MANIFEST.parent,
                             capture_output=True, text=True, check=True).stdout.split("\n")
    assert manifest.unapproved_media(manifest.select([t for t in tracked if t], m), m) == []


def test_real_manifest_names_docs_ops_files_explicitly():
    """docs/ops can hold deployment-specific notes next to the public runbooks,
    so the manifest names each exported runbook; a NEW docs/ops file is private
    until someone adds it by name."""
    m = manifest.load_manifest(REAL_MANIFEST)
    assert not any(p.startswith("docs/ops/") and "*" in p for p in m.include)
    assert manifest.select(["docs/ops/new-box-notes.md", "docs/ops/sub/x.md"], m) == []
    assert manifest.select(["docs/ops/tenant-onboarding.md",
                            "docs/ops/backup-restore.md"], m) == [
        "docs/ops/tenant-onboarding.md", "docs/ops/backup-restore.md"]
