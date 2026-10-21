"""NUL bytes in stored text.

Postgres `text` cannot hold U+0000 at all — psycopg raises "PostgreSQL text fields cannot
contain NUL (0x00) bytes" before the row is even sent. A note containing U+0000 (say, a
literal NUL inside a code sample) would therefore fail every ingest tick, never land in
the KB, and fill the log with identical error lines, if nothing between the adapter and
Postgres handled it.

The engine's write paths now swap U+0000 for U+2400 (SYMBOL FOR NULL, ␀) so such a
note still lands, readably, and re-syncs of the same bytes stay a no-op."""
from kb_core.domain import NodePatch, NodeRecord
from kb_core.validation import sanitize_text


def _rec(**over):
    base = dict(source="agent", external_id="nul", type="note",
                content="hello world", slug="nul", parent_path="")
    base.update(over)
    return NodeRecord(**base)


# ---- pure helper --------------------------------------------------------------

def test_sanitize_text_replaces_nul_with_symbol_for_null():
    assert sanitize_text("key = `${a}\x00${b}`") == "key = `${a}␀${b}`"


def test_sanitize_text_passes_through_none_and_clean_text():
    assert sanitize_text(None) is None
    assert sanitize_text("no nul here") == "no nul here"


# ---- engine write paths -------------------------------------------------------

def test_upsert_stores_nul_bytes_as_symbol_for_null(kb):
    node = kb.upsert(_rec(external_id="nul1", slug="nul1",
                          content="return `${contactId}\x00${column}`;", title="T\x00itle"))
    assert node.content == "return `${contactId}␀${column}`;"
    assert node.title == "T␀itle"


def test_reupsert_of_nul_content_is_idempotent(kb, pool):
    first = kb.upsert(_rec(external_id="nul2", slug="nul2", content="a\x00b"))
    again = kb.upsert(_rec(external_id="nul2", slug="nul2", content="a\x00b"))
    assert again.content_hash == first.content_hash
    with pool.connection() as conn:
        events = conn.execute(
            "SELECT event FROM ingest_log WHERE node_id = %s ORDER BY at", (first.id,)
        ).fetchall()
    assert [e["event"] for e in events] == ["node_created"]   # no spurious node_updated


def test_update_stores_nul_bytes_as_symbol_for_null(kb):
    node = kb.upsert(_rec(external_id="nul3", slug="nul3"))
    updated = kb.update(node.id, NodePatch(content="x\x00y", title="t\x00"))
    assert updated.content == "x␀y"
    assert updated.title == "t␀"
