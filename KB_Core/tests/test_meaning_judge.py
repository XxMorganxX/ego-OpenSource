from kb_core.meaning_judge import build_meaning_prompt, parse_meaning, MEANING_SCHEMA


def test_parse_meaning_reads_keep_and_reason():
    assert parse_meaning('{"keep": true, "reason": "named tool"}') == (True, "named tool")
    assert parse_meaning('{"keep": false, "reason": "a function"}') == (False, "a function")


def test_schema_requires_keep_and_reason():
    assert MEANING_SCHEMA["required"] == ["keep", "reason"]


def test_prompt_includes_name_and_context_and_rule():
    system, user = build_meaning_prompt(
        "build_pick_list", "tool", ["design.md: build_pick_list builds the nightly pick list"])
    assert "build_pick_list" in user
    assert "build_pick_list builds the nightly pick list" in user
    assert "keep" in system.lower()          # states the JSON verdict field
    assert "code" in system.lower()           # states the drop-code-symbols rule


def test_prompt_keeps_salient_concepts_and_drops_generic_nouns():
    system, _ = build_meaning_prompt(
        "reward hacking", "concept", ["Evaluation agents can learn reward hacking patterns"])
    text = system.lower()

    assert "salient concept" in text
    assert "generic noun" in text
    assert "retrieval" in text


def test_system_prompt_comes_from_the_file_and_accepts_an_override():
    from kb_core.meaning_judge import SYSTEM_FILE, default_system
    assert SYSTEM_FILE == "meaning_judge.system.md"
    shipped = default_system()
    assert "orchard_tools" in shipped
    system, _ = build_meaning_prompt("x", "tool", [])
    assert system == shipped
    system, _ = build_meaning_prompt("x", "tool", [], system="OVERRIDE")
    assert system == "OVERRIDE"
