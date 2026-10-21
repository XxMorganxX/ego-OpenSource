"""Row's display fields, and that both strategies actually FILL them.

contract.Row was a 1:1 port of kb_core.domain.SearchResult, and SearchResult is not
what the deployed MCP tool emits: KB_MCP's `_hit_dict` puts SEVEN fields on the wire
that Row could not express -- type, tags, source, uri, summary, importance,
dead_reason. The design says that if search_graph is not compelling,
agents default to search_memories and mode B becomes dead code with a maintenance
bill; shipping it visibly leaner is how that happens. `summary` is the worst loss:
_hit_dict's own docstring makes it load-bearing precisely BECAUSE rows deliberately
omit the note body.

SCOPE, AND WHY THE PARITY ASSERTION IS NOT HERE. This file proves POPULATION: each
new field exists, is defaulted, and carries the NODE's value in BOTH strategies. The
superset assertion against `_hit_dict`'s actual keys lives in
KB_MCP/tests/test_search_graph.py (test_search_graph_rows_carry_everything_
search_memories_rows_carry), because a KB_Retrieval test must not reach into KB_MCP
-- not by import, not by reading its source. The dependency direction is
KB_MCP -> KB_Retrieval -> KB_Core and it is acyclic IN FACT (a review
grepped for it); inverting that here would break it in the one place nobody greps,
and would fail the moment this suite ran from an installed wheel rather than the
monorepo checkout. KB_MCP can call `_hit_dict` on a real Node directly, which is a
better derivation than anything reachable from this side.
"""

from dataclasses import fields

from kb_core.domain import NodePatch, NodeRecord

from kb_retrieval.contract import Query, Row
from kb_retrieval.registry import get_strategy

# The 7 display fields Row gained. Named here so a test can say WHICH one went
# missing, rather than only that something is absent. This list is a LOCAL
# convenience, not the parity contract -- the contract is derived from _hit_dict's
# real output, over in KB_MCP's suite.
DISPLAY_FIELDS = ("type", "tags", "source", "uri", "summary", "importance",
                  "dead_reason")


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _rich_note(kb, ext, content, *, title, dead_reason=None):
    """A note with a DISTINCT, non-None value in every writable display field, so an
    all-None Row cannot pass the population tests by accident.

    `dead_reason` is the one display field nothing above can set to non-None: NodePatch
    has no dead_at/dead_reason columns, so a note built only through upsert/update/
    set_importance always carries dead_reason=None -- IDENTICAL to Row's own dataclass
    default. That makes "populated from the node" and "left at the default"
    indistinguishable for this field alone; deleting `dead_reason=node.dead_reason` from
    either strategy's Row(...) is invisible to these tests without it. Pass a reason to
    retire the note through KnowledgeBase.mark_dead -- the real path the rest of the repo
    uses (KB_Core/src/kb_core/knowledge_base.py) -- so the field actually forces the
    question. include_dead defaults True on Query (contract.py), so a dead note still
    reaches both arms; this does not need a second, undead note."""
    node = _note(kb, ext, content, title=title)
    kb.update(node.id, NodePatch(summary="one-line summary of the note",
                                 uri="https://example.invalid/n",
                                 tags=["alpha", "beta"],
                                 doc_kind="meeting notes"))
    kb.set_importance(node.id, 4.0)
    if dead_reason is not None:
        kb.mark_dead(node.id, reason=dead_reason)
    return kb.get(node.id)


def _assert_row_matches_node(row, node):
    """Every display field on the Row equals the same field on the Node. Compared
    field-by-field against the node rather than against literals, so a schema change
    that alters a value does not make this test lie."""
    for name in DISPLAY_FIELDS:
        assert getattr(row, name) == getattr(node, name), name


# ---------------------------------------------------------------------------
# the fields exist, and are additive
# ---------------------------------------------------------------------------

def test_row_declares_all_seven_display_fields():
    """Named individually so a failure says WHICH field regressed, rather than only
    that something is absent. The superset claim against _hit_dict's real keys is
    KB_MCP's to make (see this module's docstring); this is the local half."""
    row_fields = {f.name for f in fields(Row)}
    for name in DISPLAY_FIELDS:
        assert name in row_fields, name


def test_the_seven_new_fields_are_defaulted_so_nothing_existing_breaks():
    """Additive by construction: every existing construction site passes the first
    five positionally or by keyword and nothing else. If any of the seven were
    required, every Row(...) in the tree -- and every test fake -- would raise."""
    row = Row(id=None, path="a", title=None, similarity=None, rank=0.0)
    assert (row.type, row.source, row.uri, row.summary, row.importance,
            row.dead_reason) == (None, None, None, None, None, None)
    assert row.tags == []
    # A mutable default must not be shared between instances.
    row.tags.append("x")
    assert Row(id=None, path="b", title=None, similarity=None, rank=0.0).tags == []


# ---------------------------------------------------------------------------
# both strategies populate them, from the node they already hold
# ---------------------------------------------------------------------------

def test_the_hybrid_row_populates_every_display_field_from_the_node(kb):
    """hybrid.py already has the full Node -- `node, snippet = hit` from hydrate() --
    so this is zero extra round trips. Mode A is the tool search_memories deploys,
    so its rows are the reference shape the crawl has to match."""
    node = _rich_note(kb, "rich", "postgres tuning notes", title="Rebuild note",
                      dead_reason="superseded by the rebuild note")
    res = get_strategy("hybrid", kb=kb).search(Query(text="postgres"))
    row = next(r for r in res.rows if r.id == node.id)
    _assert_row_matches_node(row, node)
    # Non-vacuous: the writable ones are genuinely set.
    assert row.summary == "one-line summary of the note"
    assert row.tags == ["alpha", "beta"]
    assert row.uri == "https://example.invalid/n"
    assert row.type == "note"
    assert row.source == "agent"


def test_the_crawl_row_populates_every_display_field_from_the_node(kb, make_concept):
    """The one that actually matters: search_graph reads THIS row. Same node, same
    seven fields, same values -- so an agent gets the same information from either
    tool and mode B is not the visibly poorer product the design warns about."""
    node = _rich_note(kb, "rich", "postgres tuning notes", title="Rebuild note",
                      dead_reason="superseded by the rebuild note")
    make_concept("Postgres", [(node.id, "memory", 1.0)])
    res = get_strategy("cluster-crawl", kb=kb).search(Query(text="postgres"))
    row = next(r for r in res.rows if r.id == node.id)
    _assert_row_matches_node(row, node)
    assert row.summary == "one-line summary of the note"
    assert row.tags == ["alpha", "beta"]


def test_the_two_strategies_agree_on_every_display_field_for_the_same_note(
        kb, make_concept):
    """The strongest cheap form of the claim: whatever the value, BOTH modes report
    it identically. A benchmark's cost column measures tokens over the rows each mode
    returns, so a row shape that differs by mode makes that column less comparable
    than it looks -- which is why this is pinned before any benchmark reads it."""
    node = _rich_note(kb, "rich", "postgres tuning notes", title="Rebuild note")
    make_concept("Postgres", [(node.id, "memory", 1.0)])
    q = Query(text="postgres")
    flat = next(r for r in get_strategy("hybrid", kb=kb).search(q).rows
                if r.id == node.id)
    crawl = next(r for r in get_strategy("cluster-crawl", kb=kb).search(q).rows
                 if r.id == node.id)
    for name in DISPLAY_FIELDS:
        assert getattr(flat, name) == getattr(crawl, name), name
