from datetime import datetime, timezone

from kb_core.adapters.obsidian.parser import (
    parse_note, placement_for, derive_title, derive_tags, derive_event_at,
)


def test_parse_note_splits_frontmatter():
    meta, body = parse_note("---\ntitle: Hello\ntags: [a, b]\n---\nbody here")
    assert meta["title"] == "Hello"
    assert body.strip() == "body here"


def test_parse_note_without_frontmatter():
    meta, body = parse_note("just body")
    assert meta == {}
    assert body.strip() == "just body"


def test_parse_note_ignores_decorative_dash_separators():
    # A long dash run is a horizontal rule, not a `---` fence — and the prose between two
    # such rules must NOT be YAML-parsed (regression: "mapping values are not allowed").
    text = ("\n----------------\nShopping list.\n\nRemember:\nbuy milk\n"
            "----------------\nbody\n")
    meta, body = parse_note(text)
    assert meta == {}
    assert body == text                                  # whole text preserved as body


def test_parse_note_requires_fence_on_first_line():
    # Leading blank line before `---` means no frontmatter (Obsidian requires line 1).
    meta, body = parse_note("\n---\ntitle: Hello\n---\nbody")
    assert meta == {}
    assert "title: Hello" in body


def test_placement_for_nested():
    assert placement_for("Projects/KB MCP/design.md") == ("Projects.KB_MCP", "design")


def test_placement_for_root():
    assert placement_for("note.md") == ("", "note")


def test_derive_title_prefers_frontmatter_then_h1_then_stem():
    assert derive_title({"title": "FM"}, "# H1\nx", "stem") == "FM"
    assert derive_title({}, "# H1 title\nx", "stem") == "H1 title"
    assert derive_title({}, "no heading", "stem") == "stem"


def test_derive_tags_handles_list_and_string():
    assert derive_tags({"tags": ["a", "b"]}) == ["a", "b"]
    assert derive_tags({"tags": "a, b"}) == ["a", "b"]
    assert derive_tags({}) == []


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def test_derive_event_at_from_frontmatter_date_yaml_object():
    # YAML parses a bare ISO date to a datetime.date — the meeting-note case.
    meta, _ = parse_note("---\ndate: 2025-11-04\n---\nbody")
    assert derive_event_at(meta, "anything") == _utc(2025, 11, 4)


def test_derive_event_at_from_string_and_datetime_values():
    assert derive_event_at({"date": "2026-03-25"}, "x") == _utc(2026, 3, 25)
    assert derive_event_at({"date": "2026-03-25T14:30:00Z"}, "x") == datetime(
        2026, 3, 25, 14, 30, tzinfo=timezone.utc)
    aware = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    assert derive_event_at({"date": aware}, "x") == aware


def test_derive_event_at_key_priority_and_ignored_keys():
    # date > completed_at > created; due/updated/synced_at are never event signals.
    meta = {"date": "2026-05-01", "completed_at": "2026-06-01", "created": "2026-07-01"}
    assert derive_event_at(meta, "x") == _utc(2026, 5, 1)
    assert derive_event_at({"completed_at": "2026-06-01"}, "x") == _utc(2026, 6, 1)
    assert derive_event_at({"due": "2026-06-02", "synced_at": "2026-06-03",
                            "updated": "2026-06-04"}, "x") is None


def test_derive_event_at_unparseable_value_falls_through():
    # "TBD" isn't a date — fall to the next key, then to the filename.
    assert derive_event_at({"date": "TBD", "created": "2025-09-14"}, "x") == _utc(2025, 9, 14)
    assert derive_event_at({"date": "TBD"}, "2025-09-30 - Review") == _utc(2025, 9, 30)


def test_derive_event_at_from_filename_stem():
    assert derive_event_at({}, "2025-11-04 - Garden planning") == _utc(2025, 11, 4)
    assert derive_event_at({}, "2025_10_09_Kickoff_Notes") == _utc(2025, 10, 9)


def test_derive_event_at_no_signal_or_bogus_filename_date_is_none():
    assert derive_event_at({}, "call-the-plumber 2") is None
    assert derive_event_at({}, "2026_99_99_not_a_date") is None
    assert derive_event_at({"date": 2026}, "plain") is None   # a bare year isn't an event
