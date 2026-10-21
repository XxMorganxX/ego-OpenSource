"""Tests for list/approve/reject merge suggestion review methods."""
import threading
import uuid



def _stage(kb, kind="instance"):
    a = kb.upsert_entity("Acme"); b = kb.upsert_entity("Acme University")
    cid = kb.stage_merge_candidate(*sorted([a.node.id, b.node.id], key=str),
                                   kind, 0.1, "same uni", b.node.id)
    return a, b, cid


def test_list_shows_pending(kb):
    a, b, cid = _stage(kb)
    out = kb.list_merge_suggestions()
    assert len(out) == 1 and out[0]["id"] == cid
    assert {out[0]["left"]["name"], out[0]["right"]["name"]} == {"Acme", "Acme University"}


def test_approve_merges_and_resolves(kb):
    a, b, cid = _stage(kb)
    survivor = kb.approve_merge(cid, target_id=b.node.id)
    assert survivor.node.id == b.node.id
    assert kb.get_entity(a.node.id) is None
    assert kb.list_merge_suggestions() == []          # no longer pending


def test_reject_blocks_pair(kb):
    a, b, cid = _stage(kb)
    assert kb.reject_merge(cid, reason="different") is True
    assert kb.list_merge_suggestions() == []
    with kb._pool.connection() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM entity_merge_blocks "
                         "WHERE origin = 'human'").fetchone()["n"]
    assert n == 1


def test_approve_records_audit_row(kb):
    """Approving a merge writes one entity_merges audit row and leaves no pending candidate."""
    a, b, cid = _stage(kb)
    source_name = a.canonical_name   # will be tombstoned

    survivor = kb.approve_merge(cid, target_id=b.node.id)

    # Existing behaviour: correct survivor, source gone, candidate no longer pending.
    assert survivor is not None
    assert survivor.node.id == b.node.id
    assert kb.get_entity(a.node.id) is None
    assert kb.list_merge_suggestions() == []

    # New behaviour: one audit row in entity_merges.
    with kb._pool.connection() as conn:
        rows = conn.execute(
            "SELECT survivor_id, source_name, kind FROM entity_merges"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["survivor_id"] == b.node.id
    assert r["source_name"] == source_name
    assert r["kind"] == "instance"


def test_approve_merge_serializes_concurrent_approval_of_the_same_candidate(kb):
    """The TOCTOU this closes: approve_merge used to SELECT (no FOR UPDATE) in one
    transaction, then merge in a second, separate one. Two concurrent approve_merge(cid)
    calls for the SAME id both read status='pending' before either had claimed it, and
    both proceeded to merge_instances/merge_classes -- which are individually idempotent
    (ON CONFLICT DO NOTHING links, a DELETE that matches zero rows the second time) and
    so BOTH returned a non-None survivor, silently writing TWO entity_merges audit rows
    for one merge. This was unreachable while MCP tools ran on the event loop, whose
    in-process serialisation was the only guard; worker threads removed it.

    Deterministic construction (no sleep-based race, no chance of passing for the wrong
    reason): a raw connection manually enters the exact critical section approve_merge's
    claim step now guards -- BEGIN; SELECT ... FOR UPDATE ... WHERE status='pending' --
    and holds it open. A background thread then makes a REAL kb.approve_merge(cid, ...)
    call. Two assertions prove the fix, not just its absence of a crash:

      1. The real call is genuinely BLOCKED while the raw connection holds the lock
         (thread.join(timeout=...) has not finished). Pre-fix, approve_merge's SELECT
         carries no FOR UPDATE, so this call would NOT block here at all -- it would
         read status='pending' immediately and race ahead into a real merge.
      2. Once the raw connection claims the row (status='approving') and commits --
         simulating the FIRST caller finishing its own claim -- the blocked call
         unblocks, sees the claim, and returns None. Pre-fix, it would instead have
         already been mid-merge by this point, and this repo's audit table would show
         two rows for one candidate rather than the zero this test asserts (the raw
         connection never actually performs a merge, only the claim)."""
    a, b, cid = _stage(kb)

    with kb._pool.connection() as holder:
        row = holder.execute(
            "SELECT id FROM entity_merge_candidates WHERE id = %s AND status = 'pending' "
            "FOR UPDATE", (cid,)).fetchone()
        assert row is not None            # lock acquired, transaction still open

        result: dict = {}

        def _concurrent_approve():
            result["survivor"] = kb.approve_merge(cid, target_id=b.node.id)

        t = threading.Thread(target=_concurrent_approve)
        t.start()
        # The concurrent call's FOR UPDATE must be blocked on `holder`'s open
        # transaction -- if it finished this fast, it never contended for the lock.
        t.join(timeout=0.5)
        assert t.is_alive(), (
            "approve_merge did not block on the held row lock -- FOR UPDATE regressed")

        # Simulate the FIRST caller finishing its claim: same UPDATE approve_merge
        # itself runs, then release the lock.
        holder.execute(
            "UPDATE entity_merge_candidates SET status = 'approving' WHERE id = %s", (cid,))
        holder.commit()

        t.join(timeout=5)
        assert not t.is_alive()

    # The blocked (second, concurrent) caller saw the claim and backed off cleanly.
    assert result["survivor"] is None
    with kb._pool.connection() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM entity_merges").fetchone()["n"]
    assert n == 0                          # no merge, no audit row, from the loser
    with kb._pool.connection() as conn:
        status = conn.execute(
            "SELECT status FROM entity_merge_candidates WHERE id = %s", (cid,)
        ).fetchone()["status"]
    assert status == "approving"           # the raw connection's claim, untouched


def test_approve_rejects_bad_target_override(kb):
    """approve_merge with a target_id that is neither candidate side returns None + no audit row."""
    a, b, cid = _stage(kb)
    unrelated_id = uuid.uuid4()

    result = kb.approve_merge(cid, target_id=unrelated_id)

    assert result is None

    # No audit row written.
    with kb._pool.connection() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM entity_merges").fetchone()["n"]
    assert n == 0

    # Candidate is still pending (merge never happened).
    suggestions = kb.list_merge_suggestions()
    assert len(suggestions) == 1 and suggestions[0]["id"] == cid
