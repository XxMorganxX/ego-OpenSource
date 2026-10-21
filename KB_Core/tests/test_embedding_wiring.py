"""_build_enrichers wires embedding independently of LLM passes and honors KB_EMBED_ENABLED."""

from kb_core.ingest.daemon import _build_enrichers
from kb_core.ingest.embedding import EmbeddingEnricher


def test_embedding_runs_even_with_no_llm_passes(tmp_path):
    env = {"KB_ENRICHMENT_CONFIG": str(tmp_path / "absent.toml")}   # -> no passes
    enrichers = _build_enrichers(env)
    assert [type(e).__name__ for e in enrichers] == ["EmbeddingEnricher"]
    assert isinstance(enrichers[0], EmbeddingEnricher)


def test_no_enrichers_when_embedding_disabled_and_no_passes(tmp_path):
    env = {"KB_ENRICHMENT_CONFIG": str(tmp_path / "absent.toml"), "KB_EMBED_ENABLED": "0"}
    assert _build_enrichers(env) == []


def test_llm_then_embedding_order_when_both_active(tmp_path, monkeypatch, routing_toml):
    cfg = tmp_path / "enrichment.toml"
    # pass names must be registered inference tasks now (fail-loud routing), so the
    # ordering probe rides a real one; its target stays 'summary'.
    cfg.write_text(
        '[[pass]]\nname = "classify_memory"\nsystem = "s"\nuser = "{content}"\n'
        'target = "summary"\nphase = "enrich"\n'
    )
    import kb_core.ingest.daemon as daemon
    monkeypatch.setattr(daemon, "_preflight_inference", lambda *a, **k: None)
    enrichers = daemon._build_enrichers({"KB_ENRICHMENT_CONFIG": str(cfg),
                                         "KB_INFERENCE_ROUTING": routing_toml})
    assert [type(e).__name__ for e in enrichers] == ["LLMEnricher", "EmbeddingEnricher"]


def test_build_enrichers_resolves_passes_through_inference_toml(tmp_path, monkeypatch,
                                                                routing_toml):
    cfg = tmp_path / "enrichment.toml"
    cfg.write_text(
        '[[pass]]\nname = "entity_gate"\nsystem = "g"\nuser = "{content}"\n'
        '[[pass]]\nname = "extract_entities"\nsystem = "e"\nuser = "{content}"\n'
    )
    import kb_core.ingest.daemon as daemon
    from kb_core.ingest.claude_cli_client import ClaudeCliInferenceClient
    monkeypatch.setattr(daemon, "_preflight_inference", lambda *a, **k: None)

    enricher = daemon._build_enrichers({
        "KB_ENRICHMENT_CONFIG": str(cfg),
        "KB_INFERENCE_ROUTING": routing_toml,
        "KB_EMBED_ENABLED": "0",
    })[0]

    assert set(enricher._clients_by_pass) == {"entity_gate", "extract_entities"}
    # both ride the claude-cli tier in the fixture -> one shared client
    assert isinstance(enricher._clients_by_pass["entity_gate"], ClaudeCliInferenceClient)
    assert (enricher._clients_by_pass["entity_gate"]
            is enricher._clients_by_pass["extract_entities"])
