"""Evidence selection for the entity describe/judge view.

The view grounds the one-line description an entity is born with. It must reach for the
memories that are ABOUT the entity, not the ones most recently touched that merely name it.
"""
from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS, mention_snippet


def _mem(kb, eid, title, content, parent_path=""):
    return kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                                title=title, content=content, slug=eid,
                                parent_path=parent_path)).id


# --- mention_snippet: pure, no DB -------------------------------------------------

def test_snippet_skips_leading_heading_and_metadata_lines():
    content = (
        "# Neighborhood Garden Volunteer, question on compost bins\n"
        "\n"
        "**Participants:** Alex Example <alex@example.com>, jordan@example.com\n"
        "\n"
        "## 2026-02-03 09:15 - Alex Example -> jordan@example.com\n"
        "\n"
        "Hi Jordan, the compost bins arrived and two of them leak because"
    )
    assert mention_snippet(content).startswith("Hi Jordan,")


def test_snippet_keeps_a_blockquote_definition_line():
    """A readme that leads with a blockquote summary - that IS the definition."""
    content = (
        "# Example Robotics Club\n"
        "\n"
        "> **Summary:** Example Robotics Club meets on Thursdays to\n"
        "> build and race small autonomous rovers.\n"
    )
    assert mention_snippet(content).startswith("> **Summary:**")


def test_snippet_is_capped_and_handles_empty_content():
    assert mention_snippet("x" * 500, limit=200) == "x" * 200
    assert mention_snippet("") == ""
    assert mention_snippet(None) == ""


def test_snippet_falls_back_to_raw_text_when_every_line_is_boilerplate():
    """Never return nothing: a note that is only a heading still contributes its heading."""
    assert mention_snippet("# Just a heading\n\n") == "# Just a heading"


# --- _entity_judge_view ordering --------------------------------------------------

def test_view_prefers_subject_role_over_more_recent_mentions(kb):
    """A memory the entity is the SUBJECT of outranks five newer notes that merely name it."""
    ent = kb.upsert_entity("Example Robotics Club", categories=["organization"])
    about = _mem(kb, "readme", "Example Robotics Club",
                 "A hands-on hardware organization for students.")
    kb.link_entity(about, ent.node.id, MENTIONS, weight=0.9, role="subject")

    for i in range(5):
        boilerplate = _mem(kb, f"email{i}", f"Neighborhood Garden Volunteer, question {i}",
                           "Reminder: the shared compost bin rota changes next week.")
        kb.link_entity(boilerplate, ent.node.id, MENTIONS, weight=0.9)

    mentions = kb._entity_judge_view(ent.node.id)["mentions"]
    assert any("hands-on hardware organization" in m for m in mentions)


def test_view_prefers_higher_weight_when_no_role_is_set(kb):
    ent = kb.upsert_entity("pgvector", categories=["tool"])
    durable = _mem(kb, "spec", "Index design", "pgvector HNSW index build settings.")
    kb.link_entity(durable, ent.node.id, MENTIONS, weight=0.9)

    for i in range(5):
        weak = _mem(kb, f"log{i}", f"Scratch {i}", "pgvector mentioned once in passing.")
        kb.link_entity(weak, ent.node.id, MENTIONS, weight=0.3)

    mentions = kb._entity_judge_view(ent.node.id)["mentions"]
    assert any("HNSW index build settings" in m for m in mentions)


def test_view_snippet_skips_the_email_header_block(kb):
    ent = kb.upsert_entity("Acme", categories=["organization"])
    mem = _mem(kb, "thread", "Acme sponsorship",
               "# Acme sponsorship\n\n**Participants:** a@example.com, b@example.org\n\n"
               "Acme agreed to cover the spring fair tent rental.")
    kb.link_entity(mem, ent.node.id, MENTIONS, weight=0.9)

    mentions = kb._entity_judge_view(ent.node.id)["mentions"]
    assert mentions == ["Acme sponsorship: Acme agreed to cover the spring fair tent rental."]


def test_view_still_orders_by_recency_when_nothing_else_separates(kb):
    """Recency is the tiebreak, not the primary key - unchanged behavior for flat entities."""
    ent = kb.upsert_entity("Vandelay", categories=["company"])
    for i in range(6):
        mem = _mem(kb, f"n{i}", f"Note {i}", f"Vandelay item {i}")
        kb.link_entity(mem, ent.node.id, MENTIONS, weight=0.6)

    mentions = kb._entity_judge_view(ent.node.id)["mentions"]
    assert len(mentions) == 5
    assert any("Vandelay item 5" in m for m in mentions)   # newest survived
    assert not any("Vandelay item 0" in m for m in mentions)   # oldest dropped


def test_link_entity_rejects_a_role_outside_the_allowed_set(kb):
    """Same enforcement point as stage_entity_candidate: junk never reaches the column."""
    ent = kb.upsert_entity("Globex", categories=["company"])
    mem = _mem(kb, "g0", "Note", "Globex did a thing")
    kb.link_entity(mem, ent.node.id, MENTIONS, weight=0.6, role="not-a-role")

    # Stored as NULL, so it does not win the subject ordering.
    ent2 = kb.upsert_entity("Initech", categories=["company"])
    subject = _mem(kb, "i0", "Initech profile", "Initech is a software company.")
    kb.link_entity(subject, ent2.node.id, MENTIONS, weight=0.6, role="subject")
    assert kb._entity_judge_view(ent2.node.id)["mentions"]


def test_view_ranks_the_entitys_own_page_first(kb):
    """The page titled with the entity's name defines it, even with no role and an older
    timestamp. Newer signature-line mentions (role 'owner') and subject emails come after."""
    ent = kb.upsert_entity("Example Robotics Club", categories=["organization"])
    readme = _mem(kb, "readme", "Example Robotics Club",
                  "A hands-on hardware organization for students.")
    kb.link_entity(readme, ent.node.id, MENTIONS, weight=0.9)

    email = _mem(kb, "email", "Example Robotics Club - Spring Newsletter",
                 "Sharing what the club built this summer.")
    kb.link_entity(email, ent.node.id, MENTIONS, weight=0.9, role="subject")
    for i in range(5):
        app = _mem(kb, f"app{i}", f"Grant Application {i}",
                   "Our Example Robotics Club build season is why we qualify.")
        kb.link_entity(app, ent.node.id, MENTIONS, weight=0.9, role="owner")

    mentions = kb._entity_judge_view(ent.node.id)["mentions"]
    assert "hands-on hardware organization" in mentions[0]


def test_view_does_not_rank_other_roles_above_role_less_links(kb):
    """Only 'subject' is promoted. A non-subject role must not outrank a link with no role:
    `(role = 'subject') DESC NULLS LAST` put false (owner) ahead of NULL (no role)."""
    ent = kb.upsert_entity("Hooli", categories=["company"])
    for i in range(5):
        old = _mem(kb, f"sig{i}", f"Application {i}", f"Signed, a Hooli alum ({i})")
        kb.link_entity(old, ent.node.id, MENTIONS, weight=0.9, role="owner")
    newest = _mem(kb, "launch", "Launch notes", "Hooli shipped the new compression engine.")
    kb.link_entity(newest, ent.node.id, MENTIONS, weight=0.9)

    mentions = kb._entity_judge_view(ent.node.id)["mentions"]
    assert any("compression engine" in m for m in mentions)
