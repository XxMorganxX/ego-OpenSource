from kb_core.ingest.llm import load_passes, passes_for_phase


def test_extract_runs_in_both_phases():
    passes = load_passes()
    enrich = {p.name for p in passes_for_phase(passes, "enrich")}
    recrawl = {p.name for p in passes_for_phase(passes, "recrawl")}
    assert "extract_entities" in recrawl                 # unchanged
    assert "extract_entities" in enrich                  # NEW: also runs at first ingest
    assert "discover_classes" in enrich and "discover_classes" not in recrawl
    assert "classify_memory" in enrich and "classify_memory" not in recrawl
