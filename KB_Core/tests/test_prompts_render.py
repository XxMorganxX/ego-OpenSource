import pytest

from kb_core.ingest.llm import load_passes, render_template


def test_passes_load_and_render_without_missing_tokens():
    passes = load_passes()                       # reads ./enrichment.toml
    assert passes, "enrichment.toml should define passes"
    ctx = {"title": "t", "content": "c", "path": "p", "location": "l", "source": "s",
           "created": "2026-01-01", "updated": "2026-01-01", "today": "2026-01-01",
           "entity_classes": "(none yet)", "existing_instances": "(none)",
           "doc_kinds": "(none yet)"}
    # A user prompt may reference an EARLIER pass's output by that pass's name (e.g. {summary}
    # in topics.user.md); the enricher fills context[pass.name] at runtime, so mirror that here.
    for p in passes:
        ctx[p.name] = ""
    for p in passes:
        rendered = render_template(p.user, ctx)
        assert "{" not in rendered, f"unfilled token in {p.name}: {rendered}"


def test_discover_prompt_names_types_not_themes():
    text = open("prompts/discover_classes.system.md", encoding="utf-8").read().lower()
    assert "kind of thing" in text or "type of thing" in text
    # The old prompt framed classes as "concepts"; the re-aim drops that framing. (The prompt
    # may still mention "theme" in a negative instruction — "never a theme" — so don't assert
    # its absence.)
    assert "concept" not in text


def test_extract_prompt_targets_salient_retrieval_anchors_not_only_named_entities():
    passes = load_passes()
    extract = {p.name: p for p in passes}["extract_entities"]
    text = extract.system.lower()

    assert "retrieval anchor" in text
    assert "salient concepts" in text
    assert "noun phrases" in text
    assert "not every noun" in text
    assert "retrieval anchors" in extract.user.lower()
    assert "salient concepts" in extract.user.lower()


def test_extract_prompt_cannot_invent_taxonomy_labels():
    passes = load_passes()
    text = {p.name: p.system.lower() for p in passes}["extract_entities"]

    assert "do not coin" in text
    assert "leave `types` empty" in text


def test_taxonomy_prompts_reject_bad_existing_labels_and_memory_summaries():
    passes = load_passes()
    by_name = {p.name: p.system.lower() for p in passes}

    discover = by_name["discover_classes"]
    classify = by_name["classify_memory"]
    assert "malformed" in discover
    assert "dictionary definition" in discover
    assert "not a summary of this memory" in discover
    assert "malformed" in classify


def test_extract_prompt_renders_the_taxonomy_from_the_policy():
    from kb_core.policy import Policy
    from tests.legacy_policy import LEGACY_POLICY
    legacy = {p.name: p for p in load_passes(policy=LEGACY_POLICY)}["extract_entities"].system
    assert "Fixed broad categories:" in legacy and "  ai agent — " in legacy
    shipped = {p.name: p for p in load_passes(policy=Policy())}["extract_entities"].system
    assert "  ai agent — " not in shipped and "  system — " in shipped
    for text in (legacy, shipped):
        assert "{{" not in text                          # every marker expanded


def test_overlay_partial_reaches_the_extract_prompt(tmp_path):
    from kb_core.policy import Policy, Prompts
    (tmp_path / "entities.symbol_examples.md").write_text("MY SYMBOL EXAMPLES")
    passes = load_passes(policy=Policy(prompts=Prompts(str(tmp_path))))
    text = {p.name: p for p in passes}["extract_entities"].system
    assert "MY SYMBOL EXAMPLES" in text and "orchard_tools" not in text


def test_shipped_prompts_carry_no_owner_examples():
    # The owner's own examples are the secret, so they live only in the private
    # publish.deny.toml (never exported); a public checkout has no owner to leak.
    from pathlib import Path

    from kb_core.publish import deny
    deny_file = Path(__file__).resolve().parents[2] / "publish.deny.toml"
    if not deny_file.exists():
        pytest.skip("no publish.deny.toml (public checkout)")
    patterns = deny.load_patterns(deny_file)
    for p in load_passes():
        hits = [pat.pattern for pat in patterns if pat.search(p.system)]
        assert not hits, (p.name, hits)
