"""inference_routing: the task table, the toml loader's two-way validation, the router."""
import pytest

from kb_core.ingest.inference import HTTPInferenceClient, client_identity
from kb_core.inference_routing import (
    COMPLETION, EMBED, TASKS, InferenceRouter, RoutingError, load_provider_limits,
    load_routing,
)


# A complete, valid file covering every registered task (helper for mutations below).
def _valid_toml() -> str:
    lines = ['[provider.claude-cli]\nmodel = "claude-sonnet-5"\n']
    for name, spec in TASKS.items():
        provider = "local" if (spec.must_be_local or name in
                               ("doc_kind", "describe_entity", "merge_judge")) else "claude-cli"
        lines.append(f"[task.{name}]\nprovider = \"{provider}\"\n")
    return "\n".join(lines)


def _write(tmp_path, text):
    p = tmp_path / "inference.toml"
    p.write_text(text)
    return str(p)


def test_table_has_the_eleven_known_tasks():
    assert set(TASKS) == {
        "entity_gate", "discover_classes", "classify_memory", "extract_entities",
        "doc_kind", "describe_entity", "merge_judge", "meaning_gate",
        "concept_label", "curate_parse", "embed"}
    assert TASKS["embed"].kind == EMBED and TASKS["embed"].must_be_local
    assert all(s.kind == COMPLETION for n, s in TASKS.items() if n != "embed")


def test_load_routing_happy_path(tmp_path):
    routes = load_routing({}, path=_write(tmp_path, _valid_toml()))
    assert routes["extract_entities"].provider == "claude-cli"
    # provider-default model flows onto claude-cli tasks
    assert routes["extract_entities"].model == "claude-sonnet-5"
    assert routes["doc_kind"].provider == "local"
    assert routes["doc_kind"].model is None


def test_missing_task_fails_loud(tmp_path):
    bad = _valid_toml().replace('[task.merge_judge]\nprovider = "local"\n', "")
    with pytest.raises(RoutingError, match="merge_judge"):
        load_routing({}, path=_write(tmp_path, bad))


def test_unknown_task_fails_loud(tmp_path):
    bad = _valid_toml() + '\n[task.summarize_everything]\nprovider = "local"\n'
    with pytest.raises(RoutingError, match="summarize_everything"):
        load_routing({}, path=_write(tmp_path, bad))


def test_unknown_key_in_block_fails_loud(tmp_path):
    bad = _valid_toml().replace('[task.doc_kind]\nprovider = "local"',
                                '[task.doc_kind]\nprovider = "local"\nmodle = "x"')
    with pytest.raises(RoutingError, match="modle"):
        load_routing({}, path=_write(tmp_path, bad))


def test_bad_provider_fails_loud(tmp_path):
    bad = _valid_toml().replace('[task.doc_kind]\nprovider = "local"',
                                '[task.doc_kind]\nprovider = "gemini"')
    with pytest.raises(RoutingError, match="gemini"):
        load_routing({}, path=_write(tmp_path, bad))


def test_embed_must_stay_local(tmp_path):
    bad = _valid_toml().replace('[task.embed]\nprovider = "local"',
                                '[task.embed]\nprovider = "claude-cli"')
    with pytest.raises(RoutingError, match="embed"):
        load_routing({}, path=_write(tmp_path, bad))


def test_timeout_only_on_claude_cli_tasks(tmp_path):
    bad = _valid_toml().replace('[task.doc_kind]\nprovider = "local"',
                                '[task.doc_kind]\nprovider = "local"\ntimeout = 45')
    with pytest.raises(RoutingError, match="timeout"):
        load_routing({}, path=_write(tmp_path, bad))
    ok = _valid_toml().replace('[task.curate_parse]\nprovider = "claude-cli"',
                               '[task.curate_parse]\nprovider = "claude-cli"\ntimeout = 45')
    assert load_routing({}, path=_write(tmp_path, ok))["curate_parse"].timeout == 45


def test_per_task_model_beats_provider_default(tmp_path):
    ok = _valid_toml().replace(
        '[task.concept_label]\nprovider = "claude-cli"',
        '[task.concept_label]\nprovider = "claude-cli"\nmodel = "claude-fable-5"')
    assert load_routing({}, path=_write(tmp_path, ok))["concept_label"].model == "claude-fable-5"


def test_local_provider_block_takes_no_model(tmp_path):
    bad = '[provider.local]\nmodel = "gemma4:26b"\n\n' + _valid_toml()
    with pytest.raises(RoutingError, match="local"):
        load_routing({}, path=_write(tmp_path, bad))


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(RoutingError, match="nowhere"):
        load_routing({}, path=str(tmp_path / "nowhere" / "inference.toml"))


def test_router_caches_clients_across_tasks(tmp_path):
    router = InferenceRouter({}, path=_write(tmp_path, _valid_toml()))
    a = router.client_for("entity_gate")       # claude-cli:claude-sonnet-5
    b = router.client_for("classify_memory")   # same tier -> same instance
    assert a is b
    local = router.client_for("doc_kind")
    assert isinstance(local, HTTPInferenceClient)
    assert local is not a
    assert router.embedder() is local          # embed is local too -> shared instance


def test_router_is_lazy(tmp_path):
    router = InferenceRouter({}, path=_write(tmp_path, _valid_toml()))
    assert router._cache == {}                 # nothing built until first client_for


def test_client_for_rejects_embed_and_unknown(tmp_path):
    router = InferenceRouter({}, path=_write(tmp_path, _valid_toml()))
    with pytest.raises(RoutingError, match="embed"):
        router.client_for("embed")
    with pytest.raises(RoutingError, match="no_such_task"):
        router.client_for("no_such_task")


def test_retired_env_vars_warn_once_each(tmp_path, capsys):
    InferenceRouter({"KB_INFERENCE_PROVIDER": "claude-cli",
                     "KB_CONCEPT_LABEL_MODEL": "claude-sonnet-5"},
                    path=_write(tmp_path, _valid_toml()))
    out = capsys.readouterr().out
    assert "KB_INFERENCE_PROVIDER is set but no longer read" in out
    assert "KB_CONCEPT_LABEL_MODEL is set but no longer read" in out
    assert "inference.toml" in out


def test_describe_lists_every_task(tmp_path):
    router = InferenceRouter({}, path=_write(tmp_path, _valid_toml()))
    text = router.describe()
    for name in TASKS:
        assert name in text
    assert "extract_entities→claude-cli:claude-sonnet-5" in text


def test_repo_inference_toml_is_valid_and_complete():
    """The keystone: the COMMITTED file passes the full preflight, so CI pins the file
    and the TASKS table together — neither can drift without the other."""
    from pathlib import Path
    repo_file = Path(__file__).resolve().parents[1] / "inference.toml"
    routes = load_routing({}, path=str(repo_file))
    assert set(routes) == set(TASKS)
    assert routes["extract_entities"].provider == "claude-cli"   # routed off the local model
    assert routes["embed"].provider == "local"


def _with_cap(text: str, cap: str) -> str:
    """Replace the [provider.claude-cli] block's body with a concurrency line added."""
    return text.replace('[provider.claude-cli]\nmodel = "claude-sonnet-5"\n',
                        f'[provider.claude-cli]\nmodel = "claude-sonnet-5"\n{cap}\n')


def test_provider_limits_absent_is_none(tmp_path):
    limits = load_provider_limits({}, path=_write(tmp_path, _valid_toml()))
    assert limits["claude-cli"] is None


def test_provider_limits_reads_concurrency(tmp_path):
    ok = _with_cap(_valid_toml(), "concurrency = 8")
    assert load_provider_limits({}, path=_write(tmp_path, ok))["claude-cli"] == 8


@pytest.mark.parametrize("value", ["0", "-1", '"eight"', "2.5", "true"])
def test_bad_concurrency_fails_loud(tmp_path, value):
    bad = _with_cap(_valid_toml(), f"concurrency = {value}")
    with pytest.raises(RoutingError, match="integer >= 1"):
        load_provider_limits({}, path=_write(tmp_path, bad))


def test_concurrency_on_local_fails_loud(tmp_path):
    bad = _valid_toml() + '\n[provider.local]\nconcurrency = 2\n'
    with pytest.raises(RoutingError, match="KB_INFERENCE_MAX_CONCURRENCY"):
        load_provider_limits({}, path=_write(tmp_path, bad))


def test_local_still_rejects_a_model(tmp_path):
    bad = _valid_toml() + '\n[provider.local]\nmodel = "gemma"\n'
    with pytest.raises(RoutingError, match="takes no model"):
        load_provider_limits({}, path=_write(tmp_path, bad))


def test_unknown_provider_key_still_fails_loud(tmp_path):
    bad = _with_cap(_valid_toml(), "concurency = 8")   # typo
    with pytest.raises(RoutingError, match="concurency"):
        load_provider_limits({}, path=_write(tmp_path, bad))


def test_load_routing_ignores_the_concurrency_key(tmp_path):
    """load_routing's contract is unchanged — same dict, same TaskRoute fields."""
    ok = _with_cap(_valid_toml(), "concurrency = 8")
    routes = load_routing({}, path=_write(tmp_path, ok))
    assert routes["extract_entities"].model == "claude-sonnet-5"
    assert set(routes) == set(TASKS)


def test_local_client_is_never_wrapped(tmp_path):
    """The health preflight in analysis/daemon.py and ingest/daemon.py branches on
    isinstance(c, HTTPInferenceClient); a proxy would silently disable it."""
    ok = _with_cap(_valid_toml(), "concurrency = 4")
    router = InferenceRouter({}, path=_write(tmp_path, ok))
    local = router.client_for("doc_kind")
    assert isinstance(local, HTTPInferenceClient)
    assert router.embedder() is local


def test_gated_claude_cli_client_is_wrapped(tmp_path):
    ok = _with_cap(_valid_toml(), "concurrency = 4")
    router = InferenceRouter({}, path=_write(tmp_path, ok))
    gated = router.client_for("extract_entities")
    assert not isinstance(gated, HTTPInferenceClient)
    assert hasattr(gated, "_sem")


def test_two_routers_in_one_process_share_one_budget(tmp_path):
    """The ingest daemon builds three routers; they must not each get their own cap."""
    path = _write(tmp_path, _with_cap(_valid_toml(), "concurrency = 4"))
    a = InferenceRouter({}, path=path).client_for("extract_entities")
    b = InferenceRouter({}, path=path).client_for("entity_gate")
    assert a._sem is b._sem


def test_two_models_on_one_provider_share_one_budget(tmp_path):
    """Sonnet and Haiku are different clients but one subscription."""
    ok = _with_cap(_valid_toml(), "concurrency = 4").replace(
        '[task.concept_label]\nprovider = "claude-cli"\n',
        '[task.concept_label]\nprovider = "claude-cli"\nmodel = "claude-haiku-4-5-20251001"\n')
    router = InferenceRouter({}, path=_write(tmp_path, ok))
    sonnet = router.client_for("extract_entities")
    haiku = router.client_for("concept_label")
    assert sonnet is not haiku          # different (provider, model) -> different clients
    assert sonnet._sem is haiku._sem    # ...one budget


def test_describe_shows_the_cap(tmp_path):
    ok = _with_cap(_valid_toml(), "concurrency = 8")
    text = InferenceRouter({}, path=_write(tmp_path, ok)).describe()
    assert "extract_entities→claude-cli:claude-sonnet-5 [≤8]" in text
    assert "doc_kind→local" in text and "doc_kind→local [≤" not in text


def test_gated_client_keeps_a_usable_identity(tmp_path):
    """_GatedClient must forward provider/model, or
    client_identity() -- the enrichment provenance read in ingest/inference.py, consumed
    by ingest/llm.py's `used` set for the enrichment_done ingest-log detail -- goes blank
    for every gated client, since every enrichment pass now routes through claude-cli."""
    ok = _with_cap(_valid_toml(), "concurrency = 4")
    router = InferenceRouter({}, path=_write(tmp_path, ok))
    gated = router.client_for("extract_entities")
    assert client_identity(gated) is not None
    assert client_identity(gated) == client_identity(gated._inner)


def test_gated_client_is_cached_not_rebuilt_per_call(tmp_path):
    """wrap() runs once at cache-miss inside _client(), not on
    every client_for() call. Two tasks sharing one (provider, model, timeout) route must
    get back the identical wrapped object -- if wrap() moved to run per return instead,
    every other test here would still pass (they don't check identity of a gated client
    across two client_for() calls on the same route)."""
    ok = _with_cap(_valid_toml(), "concurrency = 4")
    router = InferenceRouter({}, path=_write(tmp_path, ok))
    a = router.client_for("extract_entities")
    b = router.client_for("entity_gate")   # same route: claude-cli:claude-sonnet-5
    assert a is b
