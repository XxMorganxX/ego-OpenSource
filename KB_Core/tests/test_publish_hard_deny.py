"""Private-by-construction paths: denied in CODE, whatever the manifest says.

The manifest is an editable, exported TOML; a single `**` include (or a PR that
adds one) must still never select these. Each case below uses a manifest that
includes EVERYTHING, so only the hard-deny can be what keeps the path out.
"""
import pytest

from kb_core.publish import manifest

EVERYTHING = manifest.Manifest(include=["**", "*"], exclude=[], remote="x")

DENIED = [
    # private-by-construction segments, at any depth
    "private/notes.md",
    "KB_Core/private/journal.md",
    "KB_Core/scratch/x.py",
    "evals/tmp/out.json",
    "KB_Frontend/test-results/run.json",
    "KB_Frontend/playwright-report/index.html",
    "evals/.cache/q.json",
    "KB_Core/src/Private/x.py",              # segment match is case-insensitive
    # dot-dirs other than .github/ and .claude/skills/setup-kb/
    ".claude/settings.json",
    ".claude/hooks/h.py",
    ".claude/skills/other/SKILL.md",
    "KB_Core/.claude/skills/x/SKILL.md",
    "KB_Frontend/.playwright-mcp/page.yml",
    ".superpowers/plan.md",
    "KB_Core/.venv/lib/x.py",
    "KB_Core/.github/workflows/ci.yml",       # only the ROOT .github is public
    # workflows other than ci.yml (publish-candidate.yml is private-only)
    ".github/workflows/publish-candidate.yml",
    ".github/workflows/deploy.yaml",
    # Dependabot runs on the private repo only
    ".github/dependabot.yml",
    ".github/dependabot.yaml",
    ".github/Dependabot.yml",
    # names
    "KB_Core/settings.local.toml",
    "KB_Core/prompts.local/doc_kind.md",
    "KB_Core/.env.local",
    "KB_Core/notes.private.md",
    "KB_Core/backup.dump",
    "KB_Core/kb.sqlite",
    "KB_Core/kb.SQLITE3",
    "KB_Core/cache.db",
    "KB_Core/.coverage",
    "KB_Core/CLAUDE.md",
    "CLAUDE.local.md",
    "KB_Core/claude-notes.md",
    "AGENTS.md",
    "KB_Frontend/Agents.md",
    "KB_Core/.cursorrules",
    "publish.deny.toml",
    "KB_Core/publish.deny.toml",
    "private/publish-state.json",
]

ALLOWED = [
    ".github/workflows/ci.yml",
    ".claude/skills/setup-kb/SKILL.md",
    ".claude/skills/setup-kb/refs/x.md",
    ".gitignore",                              # dotFILES are fine; only dot-DIRS deny
    "KB_Core/.env.example",
    "KB_Core/src/kb_core/publish/manifest.py",
    "KB_Core/src/kb_core/tmpl.py",             # 'tmp' is a segment rule, not a substring
    "KB_Core/src/kb_core/privacy.py",          # 'private' is a whole-token rule
    "KB_Core/src/kb_core/db.py",               # '*.db' is the suffix, not a stem
    "KB_Core/locales/x.toml",                  # '.local.' needs dots on both sides
    "docs/claude-code-setup.txt",              # claude*.md only
    "publish.manifest.toml",
]


@pytest.mark.parametrize("path", DENIED)
def test_hard_denied_never_selected(path):
    assert manifest.hard_deny_reason(path), path
    assert manifest.select([path], EVERYTHING) == []


@pytest.mark.parametrize("path", ALLOWED)
def test_ordinary_paths_still_selectable(path):
    assert manifest.hard_deny_reason(path) is None, path
    assert manifest.select([path], EVERYTHING) == [path]


def test_reason_names_the_rule():
    assert "private" in manifest.hard_deny_reason("KB_Core/private/x.md")
    assert "dot-dir" in manifest.hard_deny_reason(".claude/settings.json")
    assert "workflow" in manifest.hard_deny_reason(".github/workflows/publish-candidate.yml")
    assert "dependabot" in manifest.hard_deny_reason(".github/dependabot.yml")
