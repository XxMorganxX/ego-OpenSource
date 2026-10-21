"""Layer-2/3 personal-data scanner (deny.py)."""

from kb_core.publish import deny

DENY_TOML = """
[deny]
patterns = ["avery", "widgetco"]
"""


def test_load_patterns(tmp_path):
    p = tmp_path / "publish.deny.toml"
    p.write_text(DENY_TOML)
    pats = deny.load_patterns(p)
    assert len(pats) == 2


def test_scan_text_case_insensitive(tmp_path):
    p = tmp_path / "publish.deny.toml"
    p.write_text(DENY_TOML)
    pats = deny.load_patterns(p)
    hits = deny.scan_text("a/b.py", "x = 'Contact AVERY here'\nclean line\n", pats)
    assert len(hits) == 1
    assert hits[0].path == "a/b.py"
    assert hits[0].line_no == 1
    assert hits[0].pattern == "avery"
    assert "AVERY" in hits[0].line


def test_scan_file_reads_binaryish_content(tmp_path):
    p = tmp_path / "publish.deny.toml"
    p.write_text(DENY_TOML)
    pats = deny.load_patterns(p)
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01widgetco\x02")
    hits = deny.scan_file(tmp_path, "blob.bin", pats)
    assert len(hits) == 1 and hits[0].pattern == "widgetco"


def test_clean_file_no_hits(tmp_path):
    p = tmp_path / "publish.deny.toml"
    p.write_text(DENY_TOML)
    pats = deny.load_patterns(p)
    target = tmp_path / "ok.py"
    target.write_text("def f():\n    return 1\n")
    assert deny.scan_file(tmp_path, "ok.py", pats) == []
