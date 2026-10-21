from datetime import datetime, timezone

from kb_core.domain import NodePatch, NodeRecord


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def _note(kb, ext, event_at=None, source_created_at=None):
    return kb.upsert(NodeRecord(source="obsidian", external_id=ext, type="note",
                                content=f"body of {ext}", slug=ext,
                                event_at=event_at, source_created_at=source_created_at))


def test_entity_activity_puts_really_dated_memories_first(kb):
    """Declared event dates form the timeline; source-timestamp fallbacks trail it.

    CONTRACT CHANGE: the fallback used to interleave, so `no_signal`
    landed between March and January purely on its source timestamp. That
    timestamp is often not a date — bulk file operations can flatten most notes into a
    few ~1-second blocks covering most of the mention graph — so interleaving on it does
    not order the timeline, it invents one, and a memory with a genuine meeting date
    loses its place to an accident of file mtime. Really-dated memories now lead in
    true time order; undated ones follow (importance-ranked), still NULLs last.
    """
    e = kb.upsert_entity("Supabase")
    jan = _note(kb, "jan_meeting", event_at=_utc(2025, 1, 31))
    march = _note(kb, "march_note", event_at=_utc(2025, 3, 10))
    fallback = _note(kb, "no_signal", source_created_at=_utc(2025, 2, 15))
    dateless = _note(kb, "dateless")                       # no event, no source time
    for n, w in ((jan, 2.0), (march, 1.0), (fallback, None), (dateless, None)):
        kb.link_entity(n.id, e.node.id, "mentions", weight=w)
    rows = kb.entity_activity(e.node.id)
    assert [n.external_id for n, _, _ in rows] == [
        "march_note", "jan_meeting", "no_signal", "dateless"]
    assert rows[0][1] == "mentions" and rows[0][2] == 1.0     # link metadata rides along


def test_entity_activity_window_is_half_open(kb):
    e = kb.upsert_entity("Supabase")
    jan = _note(kb, "jan", event_at=_utc(2025, 1, 31))
    march = _note(kb, "march", event_at=_utc(2025, 3, 10))
    for n in (jan, march):
        kb.link_entity(n.id, e.node.id, "mentions")
    got = kb.entity_activity(e.node.id, since=_utc(2025, 2, 1))
    assert [n.external_id for n, _, _ in got] == ["march"]
    # until is exclusive: a window ending on the event's day excludes that event
    got = kb.entity_activity(e.node.id, until=_utc(2025, 3, 10))
    assert [n.external_id for n, _, _ in got] == ["jan"]


def test_entity_activity_filters_kind_link_type_and_scope(kb):
    supa = kb.upsert_entity("Supabase")
    other = kb.upsert_entity("Postgres")
    meeting = _note(kb, "meeting", event_at=_utc(2026, 5, 1))
    kb.update(meeting.id, NodePatch(doc_kind="meeting notes"))
    todo = _note(kb, "todo", event_at=_utc(2026, 6, 1))
    elsewhere = _note(kb, "elsewhere", event_at=_utc(2026, 6, 2))
    kb.link_entity(meeting.id, supa.node.id, "mentions")
    kb.link_entity(todo.id, supa.node.id, "belongs_to")
    kb.link_entity(elsewhere.id, other.node.id, "mentions")   # different entity: never leaks
    assert [n.external_id for n, _, _ in kb.entity_activity(supa.node.id)] == [
        "todo", "meeting"]
    got = kb.entity_activity(supa.node.id, kind="meeting notes")
    assert [n.external_id for n, _, _ in got] == ["meeting"]
    got = kb.entity_activity(supa.node.id, link_type="belongs_to")
    assert [(n.external_id, lt) for n, lt, _ in got] == [("todo", "belongs_to")]


def test_entity_activity_limit_offset_page_the_timeline(kb):
    e = kb.upsert_entity("Supabase")
    for day in (1, 2, 3):
        n = _note(kb, f"d{day}", event_at=_utc(2026, 6, day))
        kb.link_entity(n.id, e.node.id, "mentions")
    first = kb.entity_activity(e.node.id, limit=2)
    rest = kb.entity_activity(e.node.id, limit=2, offset=2)
    assert [n.external_id for n, _, _ in first] == ["d3", "d2"]
    assert [n.external_id for n, _, _ in rest] == ["d1"]
