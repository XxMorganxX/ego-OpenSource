import os

from kb_agent.scanner import scan_vault
from kb_core.adapters.obsidian.stamp import current_id, mint_id


def test_scan_stamps_new_notes_and_skips_dotfiles_and_excludes(tmp_path):
    (tmp_path / "Projects").mkdir(); (tmp_path / ".obsidian").mkdir(); (tmp_path / "Private").mkdir()
    (tmp_path / "Projects" / "a.md").write_text("# A\nbody\n")
    (tmp_path / "Projects" / ".hidden.md").write_text("x")
    (tmp_path / ".obsidian" / "cfg.md").write_text("x")
    (tmp_path / "Private" / "secret.md").write_text("x")
    (tmp_path / "b.md").write_text("---\nid: 11111111-2222-4333-8444-555555555555\n---\nB\n")
    seen = {o.rel_path: o for o in scan_vault(tmp_path, ["Private/**"])}
    assert set(seen) == {"Projects/a.md", "b.md"}
    assert seen["Projects/a.md"].external_id == mint_id("Projects/a.md")
    assert current_id((tmp_path / "Projects" / "a.md").read_text()) == mint_id("Projects/a.md")   # stamped on disk
    assert seen["Projects/a.md"].text.startswith("---\nid: ")                                    # pushed text includes it
    assert seen["b.md"].external_id == "11111111-2222-4333-8444-555555555555"
    assert seen["b.md"].sha and seen["b.md"].mtime_iso.endswith("+00:00")


def test_nested_excludes_and_a_bad_note_never_abort_the_scan(tmp_path):
    (tmp_path / "Private" / "deep").mkdir(parents=True)
    (tmp_path / "Private" / "deep" / "x.md").write_text("x")
    (tmp_path / "bad.md").write_text("---\nkey: [unclosed\n---\nbody\n")    # YAML that will not parse
    (tmp_path / "good.md").write_text("fine\n")
    assert [o.rel_path for o in scan_vault(tmp_path, ["Private/**"])] == ["good.md"]
    assert (tmp_path / "bad.md").read_text() == "---\nkey: [unclosed\n---\nbody\n"   # left untouched


def test_failed_files_and_unlistable_dirs_are_reported_and_logged_without_text(tmp_path, caplog):
    (tmp_path / "Locked").mkdir()
    (tmp_path / "Locked" / "x.md").write_text("x")
    (tmp_path / "bad.md").write_text("---\nkey: [SECRET unclosed\n---\nbody\n")
    (tmp_path / "good.md").write_text("fine\n")
    os.chmod(tmp_path / "Locked", 0o000)
    try:
        with caplog.at_level("WARNING", logger="kb-agent"):
            scan = scan_vault(tmp_path, [])
    finally:
        os.chmod(tmp_path / "Locked", 0o755)
    assert [o.rel_path for o in scan] == ["good.md"]
    assert {(e.rel_path, e.is_dir) for e in scan.errors} == {("Locked", True), ("bad.md", False)}
    assert scan.failed("Locked/x.md") and scan.failed("bad.md") and not scan.failed("good.md")
    assert not scan.failed("Locked2/y.md")                                  # a prefix, not a directory
    logged = [r.getMessage() for r in caplog.records]
    assert any("bad.md" in m for m in logged) and any("Locked" in m for m in logged)
    assert not any("SECRET" in m for m in logged)                           # YAML errors quote the line


def test_an_unlistable_vault_root_fails_everything(tmp_path):
    scan = scan_vault(tmp_path / "missing", [])
    assert list(scan) == [] and scan.errors and scan.failed("any/note.md")


def test_symlinks_are_never_followed_or_stamped(tmp_path):
    vault, outside = tmp_path / "vault", tmp_path / "outside.md"
    (vault / "Private").mkdir(parents=True); (vault / "Public").mkdir()
    (vault / "Private" / "secret.md").write_text("secret\n")
    outside.write_text("outside\n")
    (vault / "Public" / "link.md").symlink_to(vault / "Private" / "secret.md")
    (vault / "Public" / "out.md").symlink_to(outside)
    (vault / "Linked").symlink_to(vault / "Private", target_is_directory=True)
    (vault / "Public" / "real.md").write_text("real\n")
    seen = [o.rel_path for o in scan_vault(vault, ["Private/**"])]
    assert seen == ["Public/real.md"]
    assert outside.read_text() == "outside\n"                            # never stamped
    assert (vault / "Private" / "secret.md").read_text() == "secret\n"


def test_exclude_patterns_are_normalised_and_case_insensitive(tmp_path):
    (tmp_path / "Private" / "deep").mkdir(parents=True)
    (tmp_path / "Private" / "x.md").write_text("x"); (tmp_path / "Private" / "deep" / "y.md").write_text("y")
    (tmp_path / "Privateer.md").write_text("p"); (tmp_path / "keep.md").write_text("k")
    for pattern in ["/Private/**", "Private", "Private/", "private/**", "PRIVATE", "/private/"]:
        seen = sorted(o.rel_path for o in scan_vault(tmp_path, [pattern]))
        assert seen == ["Privateer.md", "keep.md"], pattern
    assert sorted(o.rel_path for o in scan_vault(tmp_path, ["**/deep"])) == ["Private/x.md", "Privateer.md", "keep.md"]
    assert sorted(o.rel_path for o in scan_vault(tmp_path, ["*.MD"])) == []


def test_dot_slash_patterns_and_unicode_normalisation_still_exclude(tmp_path):
    """Fail-closed privacy: `./Private` is how a shell user types a path, and macOS can hand back
    decomposed (NFD) names that a composed (NFC) pattern must still match, and vice versa."""
    nfd, nfc = "Privé", "Privé"
    (tmp_path / "Private").mkdir(); (tmp_path / "Private" / "x.md").write_text("x")
    (tmp_path / nfd).mkdir(); (tmp_path / nfd / "y.md").write_text("y")
    (tmp_path / "keep.md").write_text("k")
    for pattern in ["./Private", "./Private/**", ".//Private/"]:
        assert "Private/x.md" not in {o.rel_path for o in scan_vault(tmp_path, [pattern])}, pattern
    for pattern in [nfc, f"./{nfc}/**", nfd]:
        seen = sorted(o.rel_path for o in scan_vault(tmp_path, [pattern]))
        assert seen == ["Private/x.md", "keep.md"], ascii(pattern)
