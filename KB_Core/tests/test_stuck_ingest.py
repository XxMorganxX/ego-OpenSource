"""Stuck ingest records: ingest_failed / ingest_resolved events and their read model.

A record the engine refuses to store is invisible everywhere but ingest.log unless the
engine records it. These tests pin the dedup contract (one event per change, zero rows
per tick) and the "stuck" definition (latest event for the record is ingest_failed).
"""

from kb_core.domain import NodeRecord
from kb_core.identity import node_id


def _failed(kb):
    return kb.ingest_log(event="ingest_failed")


def _resolved(kb):
    return kb.ingest_log(event="ingest_resolved")


def test_first_failure_writes_ingest_failed_with_error_and_deterministic_node_id(kb):
    assert kb.log_ingest_failed("obsidian", "notes/a.md", "boom", path="notes.a") is True
    rows = _failed(kb)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "obsidian"
    assert row["external_id"] == "notes/a.md"
    assert row["node_id"] == node_id("obsidian", "notes/a.md")
    assert row["path"] == "notes.a"
    assert row["content_hash"] is None
    assert row["detail"] == {"error": "boom"}


def test_repeat_failure_with_same_error_and_open_set_writes_nothing(kb):
    open_set = kb.open_ingest_failures("obsidian")
    assert kb.log_ingest_failed("obsidian", "a", "boom", open=open_set) is True
    # the writer folds its own row into the dedup set it was handed
    assert open_set["a"]["error"] == "boom"
    open_set = kb.open_ingest_failures("obsidian")
    assert kb.log_ingest_failed("obsidian", "a", "boom", open=open_set) is False
    assert len(_failed(kb)) == 1


def test_changed_error_writes_a_new_row_and_stuck_shows_the_latest(kb):
    kb.log_ingest_failed("obsidian", "a", "first")
    kb.log_ingest_failed("obsidian", "a", "second")
    rows = _failed(kb)
    assert [r["detail"]["error"] for r in rows] == ["second", "first"]
    stuck = kb.stuck_ingest_records()
    assert len(stuck) == 1
    assert stuck[0]["source"] == "obsidian"
    assert stuck[0]["external_id"] == "a"
    assert stuck[0]["node_id"] == node_id("obsidian", "a")
    assert stuck[0]["error"] == "second"
    assert stuck[0]["since"] == rows[0]["at"]        # when the CURRENT error began


def test_a_record_that_lands_is_no_longer_stuck(kb):
    kb.log_ingest_failed("agent", "a", "boom")
    kb.upsert(NodeRecord(source="agent", external_id="a", type="note",
                         content="ok now", slug="a"))
    assert kb.stuck_ingest_records() == []
    assert kb.open_ingest_failures("agent") == {}
    assert _resolved(kb) == []                       # the node event supersedes; no extra row


def test_resolved_clears_the_record_and_records_the_reason(kb):
    kb.log_ingest_failed("agent", "a", "boom", path="notes.a")
    assert kb.log_ingest_resolved("agent", "a", reason="dismissed") is True
    assert kb.stuck_ingest_records() == []
    assert kb.open_ingest_failures("agent") == {}
    row = _resolved(kb)[0]
    assert row["node_id"] == node_id("agent", "a")
    assert row["path"] == "notes.a"                  # copied from the open failure
    assert row["detail"] == {"reason": "dismissed"}


def test_resolved_defaults_to_gone_from_source(kb):
    kb.log_ingest_failed("agent", "a", "boom")
    kb.log_ingest_resolved("agent", "a")
    assert _resolved(kb)[0]["detail"] == {"reason": "gone from source"}


def test_resolved_with_reason_landed_round_trips(kb):
    # A no-op recovery upsert (unchanged content -> no node event) resolves via this reason.
    kb.log_ingest_failed("agent", "a", "boom")
    assert kb.log_ingest_resolved("agent", "a", reason="landed") is True
    assert _resolved(kb)[0]["detail"] == {"reason": "landed"}


def test_log_ingest_failed_sanitizes_nul_bytes_in_the_error_text(kb):
    # An exception message can quote source content -- the one remaining path for a NUL
    # to reach a text parameter (the NUL note this feature exists to catch).
    kb.log_ingest_failed("agent", "a", "bad\x00byte")
    assert _failed(kb)[0]["detail"]["error"] == "bad␀byte"


def test_resolve_is_a_no_op_when_the_record_is_not_stuck(kb):
    assert kb.log_ingest_resolved("agent", "never-failed") is False
    kb.log_ingest_failed("agent", "a", "boom")
    kb.log_ingest_resolved("agent", "a")
    assert kb.log_ingest_resolved("agent", "a") is False   # already resolved
    assert len(_resolved(kb)) == 1


def test_a_record_that_fails_again_after_resolve_reopens(kb):
    kb.log_ingest_failed("agent", "a", "boom")
    kb.log_ingest_resolved("agent", "a", reason="dismissed")
    assert kb.log_ingest_failed("agent", "a", "boom") is True   # same text, but not stuck anymore
    assert [r["external_id"] for r in kb.stuck_ingest_records()] == ["a"]


def test_dedup_without_an_open_set_looks_the_record_up(kb):
    assert kb.log_ingest_failed("agent", "a", "boom") is True
    assert kb.log_ingest_failed("agent", "a", "boom") is False
    assert kb.log_ingest_failed("agent", "a", "other") is True
    assert len(_failed(kb)) == 2


def test_open_failures_are_source_scoped_and_carry_the_row_fields(kb):
    kb.log_ingest_failed("agent", "a", "boom")
    kb.log_ingest_failed("webhook", "b", "boom", path="hooks.b")
    assert set(kb.open_ingest_failures("agent")) == {"a"}
    assert set(kb.open_ingest_failures("webhook")) == {"b"}
    entry = kb.open_ingest_failures("webhook")["b"]
    assert entry["node_id"] == node_id("webhook", "b")
    assert entry["error"] == "boom"
    assert entry["path"] == "hooks.b"
    assert entry["at"] is not None
    assert {r["external_id"] for r in kb.stuck_ingest_records()} == {"a", "b"}


def test_stuck_records_are_newest_first_and_honour_limit(kb):
    kb.log_ingest_failed("agent", "a", "boom")
    kb.log_ingest_failed("agent", "b", "boom")
    assert [r["external_id"] for r in kb.stuck_ingest_records()] == ["b", "a"]
    assert len(kb.stuck_ingest_records(limit=1)) == 1
