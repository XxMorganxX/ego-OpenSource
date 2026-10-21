from kb_core.adapters.obsidian.stamp import current_id, mint_id, stamp_file, stamp_text


def test_mint_id_is_deterministic_per_path():
    assert mint_id("a/b.md") == mint_id("a/b.md")
    assert mint_id("a/b.md") != mint_id("a/c.md")


def test_current_id_reads_only_real_frontmatter():
    assert current_id("---\nid: x123\n---\nbody") == "x123"
    assert current_id("no frontmatter here") is None
    assert current_id("---\ntitle: T\n---\n") is None


def test_stamp_text_prepends_a_minimal_fence_when_none():
    assert stamp_text("# Title\nbody", "ID1") == "---\nid: ID1\n---\n\n# Title\nbody"


def test_stamp_text_inserts_one_line_into_existing_frontmatter():
    text = "---\ntitle: T\ntags: [a]\n---\nbody"
    # exactly one line added after the fence; the rest of the YAML is byte-for-byte intact
    assert stamp_text(text, "ID2") == "---\nid: ID2\ntitle: T\ntags: [a]\n---\nbody"


def test_stamp_text_is_a_noop_when_already_stamped():
    assert stamp_text("---\nid: keep\n---\nx", "NEW") is None


def test_stamp_file_writes_once_then_is_idempotent(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("# Hi\nbody", encoding="utf-8")
    first = stamp_file(p, "n.md")
    assert first == mint_id("n.md")
    assert current_id(p.read_text()) == first
    snapshot = p.read_text()
    assert stamp_file(p, "n.md") == first and p.read_text() == snapshot   # second run: no change


def test_stamp_file_never_overwrites_a_human_set_id(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("---\nid: human-set\n---\nx", encoding="utf-8")
    snapshot = p.read_text()
    assert stamp_file(p, "n.md") == "human-set"
    assert p.read_text() == snapshot                                      # file untouched
