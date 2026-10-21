"""Pure prompt/parse tests for the concept labeler — no DB, no network."""
import json

import pytest

from kb_core.concept_judge import (CONCEPT_SCHEMA, build_concept_prompt, fallback_label,
                                   parse_concept)


def test_prompt_carries_members_and_previous_name():
    sys_msg, usr_msg = build_concept_prompt(
        [("KB_Core", "repo"), ("Ollama", None)],
        [("entity denoising", "gate promotion on the judge")],
        previous_name="KB infrastructure project")
    assert "coherent" in sys_msg and "project" in sys_msg
    assert "- KB_Core (repo)" in usr_msg and "- Ollama" in usr_msg
    assert "- entity denoising: gate promotion on the judge" in usr_msg
    assert "KB infrastructure project" in usr_msg


def test_prompt_renders_filed_under_folder_and_tells_model_to_use_it():
    """Memory triples carry the note's filed-under breadcrumb as a [bracketed] prefix;
    the system prompt frames it as signal. Bare (title, snippet) pairs still render."""
    sys_msg, usr_msg = build_concept_prompt(
        [("KB_Core", "repo")],
        [("entity denoising", "gate promotion on the judge", "projects › kb_core"),
         ("loose note", "no folder", "")])
    assert "- [projects › kb_core] entity denoising: gate promotion on the judge" in usr_msg
    assert "- loose note: no folder" in usr_msg               # empty folder -> no prefix
    assert "filed under" in sys_msg


def test_parse_normalizes_kind_and_strips():
    reply = json.dumps({"coherent": True, "name": "  KB project ",
                        "kind": "Project", "description": " engine work "})
    lab = parse_concept(reply)
    assert lab == {"coherent": True, "name": "KB project", "kind": "project",
                   "description": "engine work"}
    weird = json.dumps({"coherent": False, "name": "x", "kind": "banana",
                        "description": ""})
    assert parse_concept(weird)["kind"] == "other"        # unknown kind -> other


def test_parse_raises_on_garbage():
    with pytest.raises(Exception):
        parse_concept("not json")


def test_fallback_label_names_from_top_entities():
    lab = fallback_label([("KB_Core", "repo"), ("Ollama", None), ("pgvector", None),
                          ("ignored", None)])
    assert lab["name"] == "KB_Core / Ollama / pgvector"
    assert lab["coherent"] is True and lab["kind"] == "other"
    assert fallback_label([])["name"] == "unlabeled cluster"


def test_schema_requires_all_fields():
    assert set(CONCEPT_SCHEMA["required"]) == {"coherent", "name", "kind", "description"}


def test_is_fallback_name_detects_placeholder_shapes():
    from kb_core.concept_judge import is_fallback_name

    assert is_fallback_name("KB_Core / Ollama / pgvector")
    assert is_fallback_name("unlabeled cluster")
    assert not is_fallback_name("Browser Tab Tracker Extension")


def test_prompt_carries_curation_notes():
    sys_msg, usr_msg = build_concept_prompt(
        [("Vandelay", "project")], [("t", "s")],
        notes=["Vandelay is my employer; the garden club is a hobby"])
    assert "curation notes:" in usr_msg
    assert "my employer" in usr_msg


def test_system_prompt_comes_from_the_file_and_accepts_an_override():
    from kb_core.concept_judge import SYSTEM_FILE, default_system
    assert SYSTEM_FILE == "concept_judge.system.md"
    shipped = default_system()
    assert "coherent" in shipped and "curation notes" in shipped
    system, _ = build_concept_prompt([("a", None)], [("t", "s")])
    assert system == shipped
    system, _ = build_concept_prompt([("a", None)], [("t", "s")], system="OVERRIDE")
    assert system == "OVERRIDE"
