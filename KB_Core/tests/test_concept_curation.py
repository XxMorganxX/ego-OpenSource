"""Pure NL-curation parse module — no DB, no network. Mirrors the judge modules."""
import json

import pytest

from kb_core.concept_curation import (CURATION_SCHEMA, build_curation_prompt,
                                      describe_op, parse_curation)

ROSTER = {"concepts": [{"id": "c-1", "name": "Acme Robotics Club"}],
          "entities": [{"id": "e-1", "name": "Nakatomi"},
                       {"id": "e-2", "name": "Acme Robotics Club"}]}


def test_prompt_carries_roster_and_instruction():
    sys_msg, usr_msg = build_curation_prompt(
        "Nakatomi is not part of the Acme club, split them", ROSTER)
    assert "ONLY ids from the roster" in sys_msg
    assert "concept c-1 :: Acme Robotics Club" in usr_msg
    assert "entity e-1 :: Nakatomi" in usr_msg
    assert "split them" in usr_msg


def test_parse_validates_ops_and_passes_warnings():
    reply = json.dumps({"ops": [
        {"op": "cannot_link", "left_id": "e-1", "right_id": "e-2",
         "note": "Nakatomi is a startup, not club work"}],
        "warnings": ["'the old cluster' did not match anything"]})
    out = parse_curation(reply)
    assert out["ops"][0]["op"] == "cannot_link"
    assert out["warnings"] == ["'the old cluster' did not match anything"]
    with pytest.raises(ValueError):
        parse_curation(json.dumps({"ops": [{"op": "explode"}]}))
    with pytest.raises(Exception):
        parse_curation("not json")


def test_describe_op_renders_human_lines():
    names = {"e-1": "Nakatomi", "e-2": "Acme Robotics Club",
             "c-1": "Acme Robotics Club"}
    line = describe_op({"op": "cannot_link", "left_id": "e-1", "right_id": "e-2"},
                       names)
    assert "Nakatomi" in line and "never share a concept" in line
    line2 = describe_op({"op": "rename", "concept_id": "c-1", "name": "Robotics Club"},
                        names)
    assert "rename" in line2 and "Robotics Club" in line2


def test_separate_op_parses_prompts_and_describes():
    reply = json.dumps({"ops": [
        {"op": "separate", "left_concept_id": "c-1", "right_concept_id": "c-2",
         "note": "volunteering is not billing"}]})
    out = parse_curation(reply)
    assert out["ops"][0]["op"] == "separate"
    sys_msg, _ = build_curation_prompt("x", ROSTER)
    assert "separate (left_concept_id, right_concept_id" in sys_msg
    names = {"c-1": "Garden Volunteers", "c-2": "Globex Billing Migration"}
    line = describe_op({"op": "separate", "left_concept_id": "c-1",
                        "right_concept_id": "c-2"}, names)
    assert "Garden Volunteers" in line and "Globex Billing Migration" in line
    assert "unrelated" in line


def test_schema_requires_op():
    item = CURATION_SCHEMA["properties"]["ops"]["items"]
    assert item["required"] == ["op"]
