"""Pure package validation for the skill registry: no database, no Docker."""
import pytest

from kb_core import skills
from kb_core.skills import (InvalidArgument, InvalidPackage, PackageTooLarge, decode_cursor,
                            encode_cursor, normalize_path, validate_package)


def _md(name="deploy-check", desc="Verify a deployment.", body="# Deploy check\n\nSteps.\n"):
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


def test_valid_package_derives_description_and_hashes():
    pkg = validate_package("deploy-check",
                           {"SKILL.md": _md(), "references/checklist.md": "- ok\n"})
    assert pkg.description == "Verify a deployment."
    assert list(pkg.files) == ["SKILL.md", "references/checklist.md"]      # sorted by path
    assert set(pkg.file_hashes) == set(pkg.files)
    assert len(pkg.package_hash) == 64
    # The package hash is a function of (path, content) only; insertion order is irrelevant.
    again = validate_package("deploy-check",
                             {"references/checklist.md": "- ok\n", "SKILL.md": _md()})
    assert again.package_hash == pkg.package_hash
    changed = validate_package("deploy-check",
                               {"SKILL.md": _md(), "references/checklist.md": "- no\n"})
    assert changed.package_hash != pkg.package_hash


def test_frontmatter_name_must_equal_the_skill_name():
    with pytest.raises(InvalidPackage, match="frontmatter name"):
        validate_package("deploy-check", {"SKILL.md": _md(name="other-name")})


@pytest.mark.parametrize("bad", ["Deploy", "deploy check", "-deploy", "deploy-",
                                 "deploy--check", "", "a" * 65, "déploy", "deploy\n"])
def test_name_rules_follow_the_agent_skills_spec(bad):
    with pytest.raises(InvalidPackage, match="skill name"):
        validate_package(bad, {"SKILL.md": _md(name=bad)})


@pytest.mark.parametrize("path", ["/etc/passwd", "../SKILL.md", "scripts/../x.py",
                                  "./SKILL.md", "a//b", "a\\b", "a/", "x\x00y", "",
                                  "p\ud800.txt"])
def test_paths_must_be_normalized_and_package_relative(path):
    with pytest.raises(InvalidPackage, match="path"):
        normalize_path(path)


def test_normal_paths_pass_through_unchanged():
    assert normalize_path("SKILL.md") == "SKILL.md"
    assert normalize_path("references/a b.md") == "references/a b.md"
    assert normalize_path("scripts/run.sh") == "scripts/run.sh"


def test_missing_skill_md_or_frontmatter_is_invalid():
    with pytest.raises(InvalidPackage, match="SKILL.md"):
        validate_package("deploy-check", {"README.md": "x"})
    with pytest.raises(InvalidPackage, match="frontmatter"):
        validate_package("deploy-check", {"SKILL.md": "# no frontmatter\n"})
    with pytest.raises(InvalidPackage, match="description"):
        validate_package("deploy-check", {"SKILL.md": "---\nname: deploy-check\n---\nbody\n"})
    with pytest.raises(InvalidPackage, match="YAML"):
        validate_package("deploy-check", {"SKILL.md": "---\nname: [unclosed\n---\nbody\n"})
    with pytest.raises(InvalidPackage, match="non-empty"):
        validate_package("deploy-check", {})


def test_description_whitespace_is_collapsed_and_bounded():
    pkg = validate_package("deploy-check", {"SKILL.md": _md(desc="  Verify\n  a   deployment. ")})
    assert pkg.description == "Verify a deployment."
    with pytest.raises(InvalidPackage, match="description"):
        validate_package("deploy-check", {"SKILL.md": _md(desc="d" * 1025)})


def test_size_limits_are_enforced_per_file_and_per_package():
    with pytest.raises(PackageTooLarge, match="per-file"):
        validate_package("deploy-check",
                         {"SKILL.md": _md(), "big.txt": "x" * (skills.MAX_FILE_BYTES + 1)})
    files = {"SKILL.md": _md()}
    files.update({f"f{i}.txt": "x" * skills.MAX_FILE_BYTES for i in range(5)})  # 5 x 256 KiB > 1 MiB
    with pytest.raises(PackageTooLarge, match="package is"):
        validate_package("deploy-check", files)
    too_many = {f"f{i}.txt": "x" for i in range(skills.MAX_FILES)}
    too_many["SKILL.md"] = _md()
    with pytest.raises(PackageTooLarge, match="files; the limit"):
        validate_package("deploy-check", too_many)


def test_content_must_be_text_without_nul_or_lone_surrogates():
    with pytest.raises(InvalidPackage, match="must be text"):
        validate_package("deploy-check", {"SKILL.md": _md(), "b.bin": b"\x00\x01"})
    with pytest.raises(InvalidPackage, match="NUL"):
        validate_package("deploy-check", {"SKILL.md": _md(), "n.txt": "a\x00b"})
    with pytest.raises(InvalidPackage, match="UTF-8"):
        validate_package("deploy-check", {"SKILL.md": _md(), "s.txt": "a\ud800b"})


def test_cursor_round_trips_and_rejects_foreign_values():
    assert decode_cursor(None) == 0
    assert decode_cursor("") == 0
    assert decode_cursor(encode_cursor(150)) == 150
    for bad in ["nope", encode_cursor(-1), "bzotxA==", 42]:
        with pytest.raises(InvalidArgument):
            decode_cursor(bad)


def test_error_text_starts_with_its_stable_code():
    assert str(InvalidPackage("x")) == "invalid_package: x"
    err = skills.RevisionConflict("skill 'a' is not at revision 3; reread and reconcile", 4)
    assert str(err).startswith("revision_conflict: ")
    assert "current_revision=4" in str(err) and err.current_revision == 4
    assert skills.SkillNotFound.code == "not_found"
    assert skills.RevisionNotFound.code == "revision_not_found"
    assert skills.FileNotFound.code == "file_not_found"
    assert skills.PackageTooLarge.code == "size_limit"
    assert skills.InvalidArgument.code == "invalid_argument"
