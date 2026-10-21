from kb_core.domain import NodeRecord
from kb_core.entities import BELONGS_TO
from kb_core.reset_entities import main as reset_main


def _seed(kb):
    cls = kb.upsert_entity_class("projects", active=True)
    note = kb.upsert(NodeRecord(source="agent", external_id="m1", type="note",
                                content="hello", slug="m1"))
    kb.link_entity(note.id, cls.node.id, BELONGS_TO)
    kb.stage_entity_candidate(note.id, "class", "databases")  # a candidate vote
    return note


def test_cli_wipes_entities_and_requeues(kb, pool):
    note = _seed(kb)
    assert kb.entity_class_names() == ["projects"]

    rc = reset_main(["--yes", "--no-backup", "--database-url", pool.conninfo])
    assert rc == 0
    # entity layer gone…
    assert kb.entity_class_names() == []
    assert kb.get_entity_class("projects") is None
    with pool.connection() as c:
        assert c.execute("SELECT count(*) n FROM entity_candidates").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) n FROM entity_links").fetchone()["n"] == 0
    # …memory preserved, but flagged pending for re-enrichment
    got = kb.get(note.id)
    assert got is not None and got.enrichment_status == "pending"


def test_cli_dry_run_changes_nothing(kb, pool):
    note = _seed(kb)
    rc = reset_main(["--dry-run", "--database-url", pool.conninfo])
    assert rc == 0
    assert kb.entity_class_names() == ["projects"]            # untouched
    assert kb.get(note.id).enrichment_status != "pending"


def test_cli_no_requeue_wipes_without_flagging(kb, pool):
    note = _seed(kb)
    rc = reset_main(["--yes", "--no-backup", "--no-requeue",
                     "--database-url", pool.conninfo])
    assert rc == 0
    assert kb.entity_class_names() == []                      # wiped
    assert kb.get(note.id).enrichment_status != "pending"     # not requeued


def test_cli_reports_concept_entity_member_blast_radius(kb, pool, capsys):
    """The node cascade empties every concept's entity roster; the preview and
    post-state must say so instead of hiding it."""
    _seed(kb)
    cls = kb.upsert_entity_class("projects", active=True)
    with pool.connection() as c:
        cid = c.execute("INSERT INTO concepts (name) VALUES ('proj') RETURNING id"
                        ).fetchone()["id"]
        c.execute("INSERT INTO concept_members (concept_id, node_id, member_kind, score) "
                  "VALUES (%s, %s, 'entity', 0.5)", (cid, cls.node.id))

    rc = reset_main(["--dry-run", "--database-url", pool.conninfo])
    assert rc == 0
    preview = " ".join(capsys.readouterr().out.split())   # collapse column padding
    assert "concept_entity_members 1" in preview

    rc = reset_main(["--yes", "--no-backup", "--database-url", pool.conninfo])
    assert rc == 0
    out = capsys.readouterr().out
    assert "concept_entity_members=0" in out              # cascade really emptied it
    assert "re-attaches entities to concepts automatically" in out
