from datetime import timedelta

from kb_core.adapters.vault.decoder import VaultDecoder
from kb_core.domain import NodeRecord
from kb_core.ingest.config import load_sources
from kb_core.ingest.drain import InboxDrainService
from kb_core.ingest.inbox import Inbox

ROOT_SETTINGS = {"vault": {"enabled": True, "is_enrichable": True, "period_seconds": 20},
                 "webhook": {"enabled": False}}


def _note(ext, rel="Notes/n.md", text="# N\nbody"):
    return {"external_id": ext, "rel_path": rel, "text": text, "mtime": "2026-10-09T10:00:00+00:00"}


def _inbox_status(pool, event_type):
    with pool.connection() as conn:
        rows = conn.execute("SELECT status FROM ingest_inbox WHERE source = 'vault' "
                            "AND event_type = %s ORDER BY id", (event_type,)).fetchall()
    return [r["status"] for r in rows]


def test_load_sources_builds_the_vault_push_source():
    srcs = {c.source: c for c in load_sources({"KB_VAULT_LAYER": "Mine"}, settings=ROOT_SETTINGS)}
    assert set(srcs) == {"vault"}
    v = srcs["vault"]
    assert v.mode == "push" and v.prune is False and v.is_enrichable is True
    assert v.period == timedelta(seconds=20)
    assert isinstance(v.decoder, VaultDecoder) and v.decoder._root_label == "Mine"


def test_load_sources_leaves_vault_off_by_default():
    assert load_sources({}, settings={}) == []


def test_drain_upserts_pushed_notes_under_the_layer(kb, pool):
    Inbox(pool).append("vault", external_id="e1", event_type="upsert", payload=_note("e1"))
    res = InboxDrainService(kb, pool).drain("vault", VaultDecoder())
    assert res.upserted == 1
    n = kb.get_by_path("Obsidian.Notes.n")
    assert n is not None and n.enrichment_status == "pending"


def test_manifest_prunes_exactly_the_unlisted_notes_and_records_the_generation(kb, pool):
    box = Inbox(pool)
    for e in ("keep", "gone"):
        box.append("vault", external_id=e, event_type="upsert", payload=_note(e, rel=f"Notes/{e}.md"))
    svc = InboxDrainService(kb, pool)
    svc.drain("vault", VaultDecoder())
    box.append("vault", external_id=None, event_type="manifest",
               payload={"ids": ["keep"], "generation": 7, "allow_empty": False})
    res = svc.drain("vault", VaultDecoder())
    assert res.deleted == 1 and res.errors == []
    assert kb.external_ids_for_source("vault") == {"keep"}
    assert svc.manifest_generation("vault") == 7
    assert kb.get_by_path("Obsidian.Notes") is not None          # folder still has a child


def test_stale_manifest_is_ignored(kb, pool):
    box = Inbox(pool)
    box.append("vault", external_id="a", event_type="upsert", payload=_note("a", rel="a.md"))
    svc = InboxDrainService(kb, pool)
    svc.drain("vault", VaultDecoder())
    box.append("vault", external_id=None, event_type="manifest", payload={"ids": ["a"], "generation": 5})
    svc.drain("vault", VaultDecoder())
    box.append("vault", external_id=None, event_type="manifest", payload={"ids": [], "generation": 4, "allow_empty": True})
    res = svc.drain("vault", VaultDecoder())
    assert res.deleted == 0 and kb.external_ids_for_source("vault") == {"a"}
    assert svc.manifest_generation("vault") == 5


def test_stale_manifest_is_logged_and_recorded_never_silent(kb, pool, caplog):
    """A reinstalled agent restarting its counter must not be dropped with no trace,
    disabling pruning forever. The skip is a WARNING and the inbox row carries why."""
    box = Inbox(pool)
    svc = InboxDrainService(kb, pool)
    box.append("vault", external_id=None, event_type="manifest", payload={"ids": ["a"], "generation": 500})
    box.append("vault", external_id="a", event_type="upsert", payload=_note("a", rel="a.md"))
    svc.drain("vault", VaultDecoder())
    stale = box.append("vault", external_id=None, event_type="manifest",
                       payload={"ids": ["a"], "generation": 3})
    with caplog.at_level("WARNING", logger="kb_core.ingest.drain"):
        res = svc.drain("vault", VaultDecoder())
    assert res.deleted == 0 and svc.manifest_generation("vault") == 500
    warned = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warned and "stale manifest" in warned[0].getMessage()
    assert "3" in warned[0].getMessage() and "500" in warned[0].getMessage()
    with pool.connection() as conn:
        row = conn.execute("SELECT status, error FROM ingest_inbox WHERE id = %s",
                           (stale,)).fetchone()
    assert row["status"] == "processed"
    assert row["error"] and "stale manifest generation 3" in row["error"] and "500" in row["error"]


def test_empty_manifest_is_refused_unless_allowed(kb, pool):
    """A crashed mid-scan agent must not be able to wipe the tenant."""
    box = Inbox(pool)
    box.append("vault", external_id="a", event_type="upsert", payload=_note("a", rel="a.md"))
    svc = InboxDrainService(kb, pool)
    svc.drain("vault", VaultDecoder())
    box.append("vault", external_id=None, event_type="manifest", payload={"ids": [], "generation": 9})
    res = svc.drain("vault", VaultDecoder())
    assert res.deleted == 0 and any("empty manifest" in e for e in res.errors)
    assert kb.external_ids_for_source("vault") == {"a"}
    assert _inbox_status(pool, "manifest") == ["failed"]
    box.append("vault", external_id=None, event_type="manifest", payload={"ids": [], "generation": 10, "allow_empty": True})
    res = svc.drain("vault", VaultDecoder())
    assert res.deleted == 1 and kb.external_ids_for_source("vault") == set()


def test_drain_marks_bad_vault_event_failed_and_continues(kb, pool):
    """One unsanitizable path fails its own event; the batch goes on."""
    box = Inbox(pool)
    box.append("vault", external_id="bad", event_type="upsert", payload=_note("bad", rel="Notes/🎉🎉.md"))
    box.append("vault", external_id="ok", event_type="upsert", payload=_note("ok", rel="Notes/ok.md"))
    res = InboxDrainService(kb, pool).drain("vault", VaultDecoder())
    assert res.upserted == 1 and len(res.errors) == 1
    assert kb.external_ids_for_source("vault") == {"ok"}
    assert box.claim_pending("vault") == []


def test_negative_or_malformed_generation_is_refused(kb, pool):
    """A negative/non-int generation must fail the event, never disable the stale guard."""
    box = Inbox(pool)
    box.append("vault", external_id="a", event_type="upsert", payload=_note("a", rel="a.md"))
    svc = InboxDrainService(kb, pool)
    svc.drain("vault", VaultDecoder())
    box.append("vault", external_id=None, event_type="manifest", payload={"ids": [], "generation": -1,
                                                                          "allow_empty": True})
    res = svc.drain("vault", VaultDecoder())
    assert res.deleted == 0 and any("non-negative integer" in e for e in res.errors)
    assert svc.manifest_generation("vault") is None
    box.append("vault", external_id=None, event_type="manifest", payload={"ids": ["a"], "generation": 5})
    svc.drain("vault", VaultDecoder())
    for bad in (-3, "6", 6.5, None):
        box.append("vault", external_id=None, event_type="manifest",
                   payload={"ids": [], "generation": bad, "allow_empty": True})
        res = svc.drain("vault", VaultDecoder())
        assert res.deleted == 0 and any("non-negative integer" in e for e in res.errors), bad
    assert kb.external_ids_for_source("vault") == {"a"}
    assert svc.manifest_generation("vault") == 5
    assert _inbox_status(pool, "manifest") == ["failed", "processed"] + ["failed"] * 4


def test_vault_manifest_leaves_other_sources_untouched(kb, pool):
    kb.upsert(NodeRecord(source="agent", external_id="m1", type="note",
                         title="M", content="agent memory", slug="m1"))
    box = Inbox(pool)
    box.append("vault", external_id="a", event_type="upsert", payload=_note("a", rel="a.md"))
    svc = InboxDrainService(kb, pool)
    svc.drain("vault", VaultDecoder())
    box.append("vault", external_id=None, event_type="manifest",
               payload={"ids": [], "generation": 1, "allow_empty": True})
    res = svc.drain("vault", VaultDecoder())
    assert res.deleted == 1 and kb.external_ids_for_source("vault") == set()
    assert kb.external_ids_for_source("agent") == {"m1"}
