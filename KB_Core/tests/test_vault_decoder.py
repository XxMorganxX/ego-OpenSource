from datetime import datetime, timezone

import pytest

from kb_core.adapters.obsidian.adapter import ObsidianAdapter
from kb_core.adapters.vault.decoder import MAX_TEXT_BYTES, VaultDecoder

NOTE = "---\nid: 11111111-2222-4333-8444-555555555555\ntitle: Planning\ntags: [a, b]\ndate: 2026-04-17\n---\n\n# Heading\n\nbody text\n"


def _payload(rel="Projects/KB MCP/plan.md", text=NOTE, **over):
    base = {"external_id": "11111111-2222-4333-8444-555555555555", "rel_path": rel, "text": text,
            "mtime": "2026-10-09T10:00:00+00:00", "birthtime": "2026-10-01T09:00:00+00:00"}
    base.update(over)
    return base


def test_decode_places_titles_tags_and_event_like_the_obsidian_adapter():
    rec = VaultDecoder().decode(_payload())
    assert rec.source == "vault" and rec.type == "note"
    assert rec.external_id == "11111111-2222-4333-8444-555555555555"
    assert (rec.parent_path, rec.slug) == ("Obsidian.Projects.KB_MCP", "plan")
    assert rec.title == "Planning" and rec.tags == ["a", "b"]
    assert rec.event_at == datetime(2026, 4, 17, tzinfo=timezone.utc)
    assert rec.content.startswith("# Heading")
    assert rec.uri == "vault://Projects/KB MCP/plan.md"
    assert rec.source_updated_at == datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
    assert rec.source_created_at == datetime(2026, 10, 1, 9, tzinfo=timezone.utc)


def test_root_note_lands_directly_under_the_layer_and_layer_can_be_off():
    assert VaultDecoder().decode(_payload(rel="note.md")).parent_path == "Obsidian"
    assert VaultDecoder(root_label=None).decode(_payload(rel="note.md")).parent_path == ""


def test_placement_matches_obsidian_adapter_for_the_same_vault(tmp_path):
    vault = tmp_path / "v"
    (vault / "Projects" / "KB MCP").mkdir(parents=True)
    (vault / "Projects" / "KB MCP" / "plan.md").write_text(NOTE)
    (vault / "loose note.md").write_text("no frontmatter here\n")
    # ObsidianAdapter's uri is path.as_uri(), which percent-encodes spaces ("KB%20MCP"), so
    # key the pulled records by slug -- identical on both sides and unique in this vault.
    pulled = {r.slug: r for r in ObsidianAdapter(str(vault), root_label="Obsidian", stamp=False).fetch()}
    assert set(pulled) == {"plan", "loose_note"}
    dec = VaultDecoder()
    for rel in ("Projects/KB MCP/plan.md", "loose note.md"):
        text = (vault / rel).read_text()
        pushed = dec.decode(_payload(rel=rel, text=text))
        want = pulled[pushed.slug]
        assert (pushed.parent_path, pushed.slug, pushed.title, pushed.tags, pushed.event_at, pushed.content) == \
            (want.parent_path, want.slug, want.title, want.tags, want.event_at, want.content)


def test_crlf_notes_match_the_pulled_adapter(tmp_path):
    """Pulls read via Path.read_text(), which turns \r\n into \n; a pushed CRLF or CR note must agree.

    python-frontmatter already normalises a note WITH frontmatter, so the plain note is the
    case that actually exercises the decoder's own normalisation.
    """
    vault = tmp_path / "v"
    vault.mkdir()
    raws = {"fm": NOTE.replace("\n", "\r\n").encode("utf-8"),
            "plain": b"# Plain\r\n\r\nline one\r\nline two\r\n",
            "mac": b"# Mac\r\rline one\rline two\r"}             # classic-Mac lone CR
    for name, raw in raws.items():
        assert b"\r" in raw
        (vault / f"{name}.md").write_bytes(raw)
    pulled = {r.slug: r for r in ObsidianAdapter(str(vault), root_label="Obsidian", stamp=False).fetch()}
    assert set(pulled) == set(raws)
    for name, raw in raws.items():
        pushed = VaultDecoder().decode(_payload(rel=f"{name}.md", text=raw.decode("utf-8")))
        assert (pushed.content, pushed.title) == (pulled[name].content, pulled[name].title)
        assert "\r" not in pushed.content


def test_missing_mtime_falls_back_to_now_and_birthtime_to_mtime():
    rec = VaultDecoder().decode(_payload(mtime=None, birthtime=None))
    assert rec.source_updated_at is not None and rec.source_created_at == rec.source_updated_at


def test_oversize_text_is_refused():
    with pytest.raises(ValueError, match="1 MiB"):
        VaultDecoder().decode(_payload(text="x" * (MAX_TEXT_BYTES + 1)))


def test_unsanitizable_rel_path_raises_value_error():
    """An all-emoji stem has no ltree-safe slug; fail THIS event readably."""
    with pytest.raises(ValueError):
        VaultDecoder().decode(_payload(rel="Projects/🎉🎉.md"))


def test_bad_yaml_is_a_value_error_not_a_crash():
    with pytest.raises(ValueError):
        VaultDecoder().decode(_payload(text="---\ntitle: [unclosed\n---\nbody\n"))


@pytest.mark.parametrize("text", [
    "---\ntitle: ok\nsecret: [PRIVATE-MARKER-xyzzy unclosed\n---\nbody\n",
    "---\ntitle: ok\nkey: PRIVATE-MARKER-xyzzy: : nope\n---\nbody\n",
    "---\n\tPRIVATE-MARKER-xyzzy: tab\n---\nbody\n",
    "---\ntitle: ok\nx: *PRIVATE-MARKER-xyzzy\n---\nbody\n",          # undefined alias: quoted
    "---\ntitle: ok\nx: !PRIVATE-MARKER-xyzzy 1\n---\nbody\n",        # unknown tag: quoted
])
def test_frontmatter_errors_carry_no_note_text(text):
    """The error reaches t.<slug>.ingest.log, the inbox row and the dashboard.
    PyYAML quotes the offending line; only the exception class and line number may pass."""
    with pytest.raises(ValueError) as exc:
        VaultDecoder().decode(_payload(text=text))
    msg = str(exc.value)
    assert "PRIVATE-MARKER" not in msg and "xyzzy" not in msg
    assert msg.startswith("frontmatter: ") and "Error" in msg
    assert exc.value.__cause__ is None and exc.value.__suppress_context__   # no chained text
    if "line" in msg:
        assert msg.split("line ", 1)[1].split()[0].rstrip(")").isdigit()
