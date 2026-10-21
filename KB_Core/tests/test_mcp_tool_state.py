"""mcp_tool_state: the inventory KB_MCP publishes and the flags the dashboard flips."""


def _publish(kb):
    kb.publish_tool_inventory([("search_memories", "read"), ("add_memory", "write")])


def test_publish_inserts_inventory(kb):
    _publish(kb)
    rows = kb.tool_states()
    # Ordering is (kind, tool), so 'read' sorts before 'write'.
    assert [(r["tool"], r["kind"]) for r in rows] == [
        ("search_memories", "read"),
        ("add_memory", "write"),
    ]
    assert all(r["enabled"] for r in rows)
    assert all(r["updated_by"] is None for r in rows)


def test_publish_is_idempotent_and_preserves_enabled(kb):
    _publish(kb)
    kb.set_tool_enabled("add_memory", False, updated_by="avery")
    # A server restart republishes the same inventory; the deliberate disable must survive.
    _publish(kb)
    states = {r["tool"]: r["enabled"] for r in kb.tool_states()}
    assert states == {"search_memories": True, "add_memory": False}


def test_publish_corrects_a_changed_kind(kb):
    _publish(kb)
    kb.set_tool_enabled("add_memory", False)
    kb.publish_tool_inventory([("add_memory", "read")])
    row = next(r for r in kb.tool_states() if r["tool"] == "add_memory")
    assert row["kind"] == "read"
    assert row["enabled"] is False


def test_set_tool_enabled_round_trips_and_stamps(kb):
    _publish(kb)
    before = next(r for r in kb.tool_states() if r["tool"] == "add_memory")
    updated = kb.set_tool_enabled("add_memory", False, updated_by="avery")
    assert updated["tool"] == "add_memory"
    assert updated["enabled"] is False
    assert updated["updated_by"] == "avery"
    assert updated["updated_at"] >= before["updated_at"]
    # And it is durable.
    assert next(r for r in kb.tool_states() if r["tool"] == "add_memory")["enabled"] is False


def test_set_tool_enabled_returns_none_for_unknown_tool(kb):
    _publish(kb)
    assert kb.set_tool_enabled("no_such_tool", False) is None
    assert len(kb.tool_states()) == 2


def test_publish_empty_inventory_is_a_noop(kb):
    kb.publish_tool_inventory([])
    assert kb.tool_states() == []


def test_tool_states_empty_before_any_publish(kb):
    assert kb.tool_states() == []
