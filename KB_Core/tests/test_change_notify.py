import json
import time

import psycopg

from kb_core.domain import NodePatch, NodeRecord


def _rec(**over):
    base = dict(source="agent", external_id="n1", type="note",
                content="hello", slug="hello", parent_path="")
    base.update(over)
    return NodeRecord(**base)


def _collect(listen, *, until_nonempty: bool, timeout: float):
    """Pump the LISTENing connection (I/O drains queued notifications to the handler)."""
    got: list = []
    listen.add_notify_handler(got.append)
    listen.execute("LISTEN kb_change")
    deadline = time.time() + timeout
    while time.time() < deadline:
        listen.execute("SELECT 1")
        if until_nonempty and got:
            break
        time.sleep(0.05)
    return got


def test_node_insert_emits_change_notify(kb, pool):
    with psycopg.connect(pool.conninfo, autocommit=True) as listen:
        listen.execute("LISTEN kb_change")
        listen.add_notify_handler(lambda n: None)  # ensure handler list exists
        got: list = []
        listen.add_notify_handler(got.append)
        kb.upsert(_rec(external_id="c1", slug="c1"))   # write on a separate (pooled) conn
        deadline = time.time() + 5
        while not got and time.time() < deadline:
            listen.execute("SELECT 1")
            time.sleep(0.05)
    assert got, "expected a kb_change notification after a node insert"
    payload = json.loads(got[0].payload)
    assert payload["table"] == "nodes"
    assert payload["op"] == "INSERT"


def test_analysis_exclude_notifies(kb, pool):
    # Flipping analysis_excluded recolors the tree icon + graph node, so it must push a
    # live refetch like any other UI-visible column change (added to the trigger in 0017).
    n = kb.upsert(_rec(external_id="x1", slug="x1"))
    with psycopg.connect(pool.conninfo, autocommit=True) as listen:
        got = _collect(listen, until_nonempty=False, timeout=0.2)  # arm + drain insert echo
        got.clear()
        kb.set_analysis_excluded(n.id, True)
        deadline = time.time() + 5
        while not got and time.time() < deadline:
            listen.execute("SELECT 1")
            time.sleep(0.05)
    assert got, "expected a kb_change notification after toggling analysis_excluded"
    payload = json.loads(got[0].payload)
    assert payload["table"] == "nodes"
    assert payload["op"] == "UPDATE"


def test_access_bump_does_not_notify(kb, pool):
    n = kb.upsert(_rec(external_id="a1", slug="a1"))
    with psycopg.connect(pool.conninfo, autocommit=True) as listen:
        got = _collect(listen, until_nonempty=False, timeout=0.2)  # arm + drain insert echo
        got.clear()
        kb.get(n.id, track_access=True)   # bumps access_count/last_accessed_at only
        deadline = time.time() + 1.5
        while time.time() < deadline:
            listen.execute("SELECT 1")
            time.sleep(0.05)
    # the access-bookkeeping UPDATE must be filtered out by the trigger's WHEN clause
    assert got == [], f"access bump should not notify, got {[g.payload for g in got]}"


def test_doc_kind_only_update_notifies(kb, pool):
    # A doc_kind-only update (the classification pass, or a manual reclassification) must
    # push a live refetch like any other UI-visible column change (added to the trigger in
    # 0030 -- 0029 added the column but the trigger's change-list predates it).
    n = kb.upsert(_rec(external_id="dk1", slug="dk1"))
    with psycopg.connect(pool.conninfo, autocommit=True) as listen:
        got = _collect(listen, until_nonempty=False, timeout=0.2)  # arm + drain insert echo
        got.clear()
        kb.update(n.id, NodePatch(doc_kind="meeting notes"))
        deadline = time.time() + 5
        while not got and time.time() < deadline:
            listen.execute("SELECT 1")
            time.sleep(0.05)
    assert got, "expected a kb_change notification after a doc_kind-only update"
    payload = json.loads(got[0].payload)
    assert payload["table"] == "nodes"
    assert payload["op"] == "UPDATE"
