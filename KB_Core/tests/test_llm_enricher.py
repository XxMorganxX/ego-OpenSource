from datetime import datetime, timezone
from pathlib import Path

import pytest

from kb_core.domain import NodeRecord
from kb_core.ingest.enrichment import Enricher, EnrichmentWorker
from kb_core.ingest.inference import InferenceClient
from kb_core.ingest.llm import LLMEnricher, LLMPass, load_passes, render_template


class FakeClient:
    """Records (system, user) calls and returns scripted replies in order."""
    def __init__(self, replies):
        self.calls = []
        self._replies = list(replies)

    def complete(self, system_prompt, user_prompt, *, schema=None):
        self.calls.append((system_prompt, user_prompt, schema))
        return self._replies.pop(0)


def _rec(**over):
    base = dict(source="agent", external_id="m1", type="note",
                content="hello world", slug="hello", title="T")
    base.update(over)
    return NodeRecord(**base)


def test_fake_client_satisfies_protocol():
    assert isinstance(FakeClient([]), InferenceClient)


def test_llm_enricher_satisfies_enricher_protocol():
    assert isinstance(LLMEnricher(FakeClient([]), []), Enricher)


def test_llm_enricher_routes_only_named_pass_to_override_client(kb):
    node = kb.upsert(_rec(external_id="route1", slug="route1"))
    default = FakeClient(["default reply"])
    local = FakeClient(["local reply"])
    passes = [LLMPass("entity_gate", "gate-system", "gate {content}"),
              LLMPass("extract_entities", "extract-system", "extract {content}")]

    LLMEnricher(
        default, passes, clients_by_pass={"extract_entities": local}
    ).enrich(node, kb)

    assert [call[0] for call in default.calls] == ["gate-system"]
    assert [call[0] for call in local.calls] == ["extract-system"]


def test_clients_for_passes_resolves_every_pass_via_router(routing_toml):
    from kb_core.inference_routing import InferenceRouter
    from kb_core.ingest.claude_cli_client import ClaudeCliInferenceClient
    from kb_core.ingest.inference import HTTPInferenceClient
    from kb_core.ingest.llm import clients_for_passes

    router = InferenceRouter({}, path=routing_toml)
    passes = [LLMPass("entity_gate", "s", "u"), LLMPass("extract_entities", "s", "u"),
              LLMPass("doc_kind", "s", "u")]

    clients = clients_for_passes(router, passes)

    assert set(clients) == {"entity_gate", "extract_entities", "doc_kind"}
    assert isinstance(clients["extract_entities"], ClaudeCliInferenceClient)
    assert isinstance(clients["doc_kind"], HTTPInferenceClient)
    assert clients["entity_gate"] is clients["extract_entities"]   # shared tier client


def test_render_substitutes_known_and_leaves_unknown():
    out = render_template("Hi {title}: {content} {unknown}", {"title": "T", "content": "C"})
    assert out == "Hi T: C {unknown}"


def test_humanize_path_makes_breadcrumb():
    from kb_core.paths import humanize_path
    assert humanize_path("projects.kb_mcp.Smoke_Test") == "projects › kb_mcp › Smoke_Test"
    assert humanize_path("") == ""         # guard: notes always have a path in practice
    assert humanize_path(None) == ""


def test_enrich_exposes_humanized_location_to_prompt(kb):
    node = kb.upsert(_rec(external_id="loc1", slug="loc1", parent_path="projects.kb_mcp",
                          title="T", content="body"))
    expected = node.path.replace(".", " › ")               # humanized breadcrumb of node.path
    passes = [LLMPass("summary", "sys", "Location: {location}\n{content}", target="summary")]
    client = FakeClient(["a summary"])
    LLMEnricher(client, passes).enrich(node, kb)
    assert f"Location: {expected}" in client.calls[0][1]


def test_load_passes_from_toml(tmp_path):
    f = tmp_path / "enrichment.toml"
    f.write_text(
        '[[pass]]\nname="summary"\ntarget="summary"\nsystem="sys"\nuser="u {content}"\n'
        '[[pass]]\nname="topics"\nsystem="s2"\nuser="from {summary}"\n'
    )
    passes = load_passes({"KB_ENRICHMENT_CONFIG": str(f)})
    assert [p.name for p in passes] == ["summary", "topics"]
    assert passes[0].target == "summary" and passes[1].target is None
    assert passes[0].user == "u {content}"


def test_missing_config_yields_no_passes():
    assert load_passes({"KB_ENRICHMENT_CONFIG": "/nope/enrichment.toml"}) == []


def test_extract_stages_plan_typed_anchor(kb):
    # Part A: a plan-typed anchor from the model stages like any other open-vocab type.
    node = kb.upsert(_rec(external_id="plan1", slug="plan1",
                          content="drafted the billing schema redesign plan"))
    passes = [LLMPass("extract", "sys", "cats: {entity_classes}\n{content}",
                      target="entities", schema={"type": "object"})]
    reply = ('{"entities": [{"types": ["plan"], '
             '"name": "billing schema redesign plan", "confidence": 0.9}]}')
    LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    staged = {(g["entity_type"], g["normalized_name"])
              for g in kb.promotable_entities(threshold=1)}
    assert ("plan", "billing schema redesign plan") in staged


def test_enrich_runs_passes_in_order_threads_outputs_and_writes_fields(kb):
    node = kb.upsert(_rec(external_id="e1", slug="e1", title="T", content="body text"))
    passes = [
        LLMPass("summary", "sys-sum", "Summarize: {content}", target="summary"),
        LLMPass("topics", "sys-top", "From summary '{summary}' list topics", target="tags"),
    ]
    client = FakeClient(["a short summary", "alpha, beta , gamma"])
    LLMEnricher(client, passes).enrich(node, kb)

    got = kb.get(node.id)
    assert got.summary == "a short summary"
    assert got.tags == ["alpha", "beta", "gamma"]            # comma-split + trimmed
    # pass 1 templated {content}; pass 2 saw pass 1's output via {summary}
    assert "body text" in client.calls[0][1]
    assert "a short summary" in client.calls[1][1]


def test_worker_runs_llm_enricher_and_logs_done(kb, pool):
    n = kb.upsert(_rec(external_id="w1", slug="w1", title="T", content="c"))
    kb.flag_stale_enrichment()
    passes = [LLMPass("summary", "s", "{content}", target="summary")]
    processed = EnrichmentWorker(kb, [LLMEnricher(FakeClient(["the summary"]), passes)]).run()
    assert processed == 1
    done = kb.get(n.id)
    assert done.enrichment_status == "done" and done.summary == "the summary"
    assert any(r["event"] == "enrichment_done" for r in kb.ingest_log())


def test_build_enrichers_empty_when_no_config():
    from kb_core.ingest.daemon import _build_enrichers
    # With embedding also disabled, no config → empty list.
    assert _build_enrichers({"KB_ENRICHMENT_CONFIG": "/nope/enrichment.toml",
                              "KB_EMBED_ENABLED": "0"}) == []


def test_build_enrichers_from_config(tmp_path, routing_toml):
    from kb_core.ingest.daemon import _build_enrichers
    f = tmp_path / "e.toml"
    # pass names must be registered inference tasks (fail-loud routing)
    f.write_text('[[pass]]\nname="classify_memory"\ntarget="summary"\nsystem="x"\nuser="{content}"\n')
    enrichers = _build_enrichers({"KB_ENRICHMENT_CONFIG": str(f), "KB_EMBED_ENABLED": "0",
                                  "KB_INFERENCE_ROUTING": routing_toml})
    assert len(enrichers) == 1 and isinstance(enrichers[0], LLMEnricher)


def test_load_passes_reads_schema_file(tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "ent.json").write_text(
        '{"type": "object", "required": ["entities"]}')
    f = tmp_path / "enrichment.toml"
    f.write_text(
        '[[pass]]\n'
        'name = "entities"\n'
        'target = "entities"\n'
        'schema_file = "schemas/ent.json"\n'   # resolved relative to the config
        'system = "s"\n'
        'user = "u"\n'
    )
    passes = load_passes({"KB_ENRICHMENT_CONFIG": str(f)})
    assert passes[0].schema == {"type": "object", "required": ["entities"]}


def test_parse_json_tolerates_code_fences():
    from kb_core.ingest.llm import _parse_json
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('{"a": 2}') == {"a": 2}
    assert _parse_json('not json at all') is None


def test_entities_pass_forwards_schema_and_stages_candidates(kb):
    kb.upsert_entity_class("person", active=True)
    node = kb.upsert(_rec(external_id="ent1", slug="ent1", content="met avery and alex"))
    schema = {"type": "object", "required": ["entities"]}
    passes = [LLMPass("entities", "sys", "Extract from {content}",
                      target="entities", schema=schema)]
    reply = ('{"entities": [{"type": "person", "name": "Avery", "confidence": 0.9},'
             ' {"type": "person", "name": "Alex", "confidence": 0.5}]}')
    client = FakeClient([reply])
    LLMEnricher(client, passes).enrich(node, kb)
    assert client.calls[0][2] == schema                       # schema forwarded (enforced)
    staged = {(g["entity_type"], g["normalized_name"])
              for g in kb.promotable_entities(threshold=1)}
    assert ("person", "avery") in staged and ("person", "alex") in staged


def test_entities_pass_stages_the_bottom_band_but_withholds_promotion(kb):
    # The 0.3 band is staged rather than dropped (it is the pass's most useful signal and was
    # invisible while ingest deleted it), but it can never create an entity on its own.
    kb.upsert_entity_class("people", active=True)
    node = kb.upsert(_rec(external_id="entlc", slug="entlc", content="x"))
    passes = [LLMPass("entities", "sys", "u {content}", target="entities",
                      schema={"type": "object", "required": ["entities"]})]
    reply = ('{"entities": ['
             '{"category": "people", "name": "Solid", "confidence": 0.9},'
             '{"category": "people", "name": "Maybe", "confidence": 0.6},'
             '{"category": "people", "name": "Guess", "confidence": 0.3}]}')
    LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    staged = {(g["entity_type"], g["normalized_name"])
              for g in kb.promotable_entities(threshold=1)}
    assert ("people", "solid") in staged and ("people", "maybe") in staged
    assert ("people", "guess") not in staged          # recorded, but never promotable
    with kb._pool.connection() as conn:
        rows = conn.execute("SELECT confidence FROM entity_candidates "
                            "WHERE normalized_name = 'guess'").fetchall()
    assert [r["confidence"] for r in rows] == [pytest.approx(0.3)]   # ...and still on the ledger


def test_load_passes_reads_prompt_files(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "s.md").write_text("the system prompt\n")
    (tmp_path / "prompts" / "u.md").write_text("hi {content}\n")
    f = tmp_path / "enrichment.toml"
    f.write_text('[[pass]]\nname="p"\nsystem_file="prompts/s.md"\nuser_file="prompts/u.md"\n')
    passes = load_passes({"KB_ENRICHMENT_CONFIG": str(f)})
    assert passes[0].system == "the system prompt"      # read from file, stripped
    assert passes[0].user == "hi {content}"


def test_inline_prompts_still_work(tmp_path):
    f = tmp_path / "enrichment.toml"
    f.write_text('[[pass]]\nname="p"\nsystem="inline sys"\nuser="inline {content}"\n')
    passes = load_passes({"KB_ENRICHMENT_CONFIG": str(f)})
    assert passes[0].system == "inline sys" and passes[0].user == "inline {content}"


def test_discover_pass_stages_inactive_classes(kb):
    node = kb.upsert(_rec(external_id="d1", slug="d1", content="met some people"))
    passes = [LLMPass("discover", "sys", "existing: {entity_classes}\n{content}",
                      target="entity_classes", schema={"type": "object"})]
    reply = ('{"classes": [{"name": "people", "status": "new"},'
             ' {"name": "projects", "status": "new"}]}')
    LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    # discovered classes exist as INACTIVE placeholders, not yet offered as categories —
    # they must recur past the threshold before activating.
    assert kb.entity_class_names() == []
    classes = {e.canonical_name: e.active for e in kb.list_entities(entity_type="class")}
    assert classes == {"people": False, "projects": False}


def test_discover_pass_caps_candidates_at_four(kb):
    node = kb.upsert(_rec(external_id="cap1", slug="cap1", content="a rich memory"))
    passes = [LLMPass("discover", "sys", "existing: {entity_classes}\n{content}",
                      target="entity_classes", schema={"type": "object"})]
    # Five valid concept names in one reply — the fake client does NOT enforce the schema,
    # so this exercises the code-level cap.
    reply = ('{"classes": ['
             '{"name": "entity resolution", "status": "new"},'
             '{"name": "knowledge graph design", "status": "new"},'
             '{"name": "memory significance scoring", "status": "new"},'
             '{"name": "prompt engineering", "status": "new"},'
             '{"name": "retrieval evaluation", "status": "new"}]}')
    LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    staged = {e.canonical_name for e in kb.list_entities(entity_type="class")}
    assert len(staged) == 4                                  # the 5th is dropped
    assert staged <= {"entity resolution", "knowledge graph design",
                      "memory significance scoring", "prompt engineering",
                      "retrieval evaluation"}


def test_classify_skips_inactive_classes(kb):
    from kb_core.entities import BELONGS_TO
    node = kb.upsert(_rec(external_id="tp1", slug="tp1", content="avery on the kb project"))
    passes = [
        LLMPass("discover", "s", "existing: {entity_classes}", target="entity_classes",
                schema={"type": "object"}),
        LLMPass("classify", "s", "defined: {entity_classes}", target="classify",
                schema={"type": "object"}),
    ]
    client = FakeClient([
        '{"classes": [{"name": "people", "status": "new"}]}',
        '{"classes": ["people"]}',
    ])
    LLMEnricher(client, passes).enrich(node, kb)
    # The just-discovered class is now surfaced to the classifier by name (so the model can
    # reuse it instead of minting a near-duplicate) — but it's still inactive, so classify
    # forms no belongs_to edge yet; its voters get backfilled when the class activates.
    assert "existing: (none yet)" in client.calls[0][1]
    assert "defined: - people" in client.calls[1][1]
    cls = kb.get_entity_class("people")
    assert cls is not None and cls.active is False
    assert kb.memories_linked(cls.node.id, link_type=BELONGS_TO) == []


def test_classify_links_to_active_class(kb):
    from kb_core.entities import BELONGS_TO
    kb.upsert_entity_class("people")                      # explicit creation -> active
    node = kb.upsert(_rec(external_id="cl1", slug="cl1", content="avery"))
    passes = [LLMPass("classify", "s", "defined: {entity_classes}", target="classify",
                      schema={"type": "object"})]
    client = FakeClient(['{"classes": ["people", "ghost"]}'])  # 'ghost' absent -> skipped
    LLMEnricher(client, passes).enrich(node, kb)
    assert "defined: - people" in client.calls[0][1]       # active class is offered
    cls = kb.get_entity_class("people")
    grouped = kb.memories_linked(cls.node.id, link_type=BELONGS_TO)
    assert [n.id for n in grouped] == [node.id]           # linked only to the active class


def test_load_passes_reads_phase_default_enrich(tmp_path):
    f = tmp_path / "enrichment.toml"
    f.write_text(
        '[[pass]]\nname="summary"\ntarget="summary"\nsystem="s"\nuser="{content}"\n'
        '[[pass]]\nname="extract"\ntarget="entities"\nphase="recrawl"\nsystem="s"\nuser="u"\n'
    )
    passes = load_passes({"KB_ENRICHMENT_CONFIG": str(f)})
    assert passes[0].phase == "enrich"          # default when unspecified
    assert passes[1].phase == "recrawl"


def test_passes_for_phase_filters():
    from kb_core.ingest.llm import passes_for_phase
    ps = [LLMPass("a", "s", "u", phase="enrich"),
          LLMPass("b", "s", "u", phase="recrawl")]
    assert [p.name for p in passes_for_phase(ps, "enrich")] == ["a"]
    assert [p.name for p in passes_for_phase(ps, "recrawl")] == ["b"]


def test_build_enrichers_filters_by_phase(tmp_path, routing_toml):
    from kb_core.ingest.daemon import _build_enrichers
    (tmp_path / "enrichment.toml").write_text(
        '[[pass]]\nname="discover_classes"\ntarget="entity_classes"\nsystem="s"\nuser="u"\n'
        '[[pass]]\nname="extract_entities"\ntarget="entities"\nphase="recrawl"\nsystem="s"\nuser="u"\n')
    env = {"KB_ENRICHMENT_CONFIG": str(tmp_path / "enrichment.toml"),
           "KB_INFERENCE_URL": "http://127.0.0.1:9",   # unreachable is fine; preflight only warns
           "KB_EMBED_ENABLED": "0",
           "KB_INFERENCE_ROUTING": routing_toml}
    enrichers = _build_enrichers(env, phase="enrich")
    assert len(enrichers) == 1
    assert [p.name for p in enrichers[0]._passes] == ["discover_classes"]   # recrawl excluded


def test_build_enrichers_empty_when_phase_has_no_passes(tmp_path):
    from kb_core.ingest.daemon import _build_enrichers
    (tmp_path / "enrichment.toml").write_text(
        '[[pass]]\nname="discover"\ntarget="entity_classes"\nsystem="s"\nuser="u"\n')
    env = {"KB_ENRICHMENT_CONFIG": str(tmp_path / "enrichment.toml"),
           "KB_EMBED_ENABLED": "0"}
    # No recrawl passes and embedding disabled → empty.
    assert _build_enrichers(env, phase="recrawl") == []


def test_extract_entities_pass_stages_with_category(kb):
    kb.upsert_entity_class("people")
    node = kb.upsert(_rec(external_id="x1", slug="x1", content="met avery"))
    passes = [LLMPass("extract", "sys", "cats: {entity_classes}\n{content}",
                      target="entities", schema={"type": "object"})]
    reply = '{"entities": [{"category": "people", "name": "Avery", "confidence": 0.9}]}'
    LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    groups = kb.promotable_entities(threshold=1)
    assert (groups[0]["entity_type"], groups[0]["normalized_name"]) == ("people", "avery")


def test_extract_stages_instance_under_coined_inactive_type(kb):
    # Open vocabulary: a type the model coins that is NOT an active class must still stage the
    # instance UNDER that type (no longer demoted to __untyped__).
    node = kb.upsert(_rec(external_id="ov1", slug="ov1", content="joined Acme"))
    passes = [LLMPass("extract", "sys", "cats: {entity_classes}\n{content}",
                      target="entities", schema={"type": "object"})]
    reply = '{"entities": [{"types": ["company"], "name": "Acme", "confidence": 0.9}]}'
    LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    staged = {(g["entity_type"], g["normalized_name"])
              for g in kb.promotable_entities(threshold=1)}
    assert ("company", "acme") in staged             # under the coined type
    assert ("__untyped__", "acme") not in staged     # not demoted


def test_extract_creates_inactive_class_for_coined_type(kb):
    # The coined type also materializes as an INACTIVE class placeholder so it can accumulate
    # votes and activate through the normal reconcile path.
    node = kb.upsert(_rec(external_id="ov2", slug="ov2", content="joined Acme"))
    passes = [LLMPass("extract", "sys", "{content}", target="entities",
                      schema={"type": "object"})]
    reply = '{"entities": [{"types": ["company"], "name": "Acme", "confidence": 0.9}]}'
    LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    cls = kb.get_entity_class("company")
    assert cls is not None and cls.active is False    # created, not yet offered as a category


def test_extract_coined_type_activates_on_recurrence(kb):
    # The class vote extraction casts for a coined type flows through reconcile_classes: once it
    # recurs across enough distinct memories, the category activates.
    reply = '{"entities": [{"types": ["company"], "name": "Acme", "confidence": 0.9}]}'
    passes = [LLMPass("extract", "sys", "{content}", target="entities",
                      schema={"type": "object"})]
    for i in range(3):
        node = kb.upsert(_rec(external_id=f"ov3-{i}", slug=f"ov3-{i}", content="joined Acme"))
        LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    kb.reconcile_classes(floor=3, cap=20, dominance_hi=0.6, dominance_lo=0.6)
    assert kb.get_entity_class("company").active is True


def test_extract_invalid_type_label_drops_to_untyped_without_class(kb):
    # The artifact guard still bites: an underscore/SCREAMING type is not a real category, so the
    # instance lands __untyped__ and NO class node is coined for it.
    node = kb.upsert(_rec(external_id="ov4", slug="ov4", content="read a paper"))
    passes = [LLMPass("extract", "sys", "{content}", target="entities",
                      schema={"type": "object"})]
    reply = ('{"entities": [{"types": ["research_paper", "NEW_CATEGORY_NAME"],'
             ' "name": "AttentionPaper", "confidence": 0.9}]}')
    LLMEnricher(FakeClient([reply]), passes).enrich(node, kb)
    staged = {(g["entity_type"], g["normalized_name"])
              for g in kb.promotable_entities(threshold=1)}
    assert ("__untyped__", "attentionpaper") in staged
    assert kb.get_entity_class("research_paper") is None
    assert kb.get_entity_class("NEW_CATEGORY_NAME") is None


def test_extract_prompt_uses_controlled_type_vocabulary():
    # Extraction is open-vocabulary for retrieval anchors but controlled for taxonomy labels:
    # new broad types come from discover_classes, not arbitrary Gemma completions.
    root = Path(__file__).resolve().parent.parent
    passes = load_passes({"KB_ENRICHMENT_CONFIG": str(root / "enrichment.toml")})
    sysp = {p.name: p for p in passes}["extract_entities"].system.lower()
    assert "semantic retrieval anchors" in sysp
    assert "fixed broad categories" in sysp
    assert "do not coin" in sysp
    assert "leave `types` empty" in sysp


def test_entity_prompts_prefer_broad_reusable_classes():
    # The fixed-category names below (repository, ai agent, research paper, …) are the
    # pre-policy-layer taxonomy (tests/legacy_policy.py), not the shipped default — the
    # taxonomy now renders from the policy, so this asserts against LEGACY_POLICY.
    from tests.legacy_policy import LEGACY_POLICY
    root = Path(__file__).resolve().parent.parent
    passes = load_passes({"KB_ENRICHMENT_CONFIG": str(root / "enrichment.toml")},
                         policy=LEGACY_POLICY)
    by_name = {p.name: p for p in passes}

    discover = by_name["discover_classes"].system.lower()
    extract = by_name["extract_entities"].system.lower()

    for broad_type in (
        "repository", "project", "product", "person", "organization",
        "documentation", "dataset", "ai agent", "research paper",
    ):
        assert broad_type in extract

    for narrow_type in ("pull request", "test case"):
        assert narrow_type in discover        # dropped `benchmark` — no longer in the neutral examples

    assert "fixed broad categories" in extract
    assert "do not coin" in extract
    assert "broad parent" in discover


def test_class_list_renders_name_and_description(kb):
    kb.upsert_entity_class("people", active=True, description="humans we know")
    kb.upsert_entity_class("ideas", active=True)  # no description -> bare name
    out = LLMEnricher(None, [])._class_list(kb)
    assert "- people — humans we know" in out
    assert "- ideas" in out


def test_entity_gate_marks_noise_and_skips_entity_passes(kb):
    node = kb.upsert(_rec(external_id="g1", slug="g1", content="asdkjh qwe;lk zzz"))
    passes = [
        LLMPass("entity_gate", "s", "{content}", target="entity_gate", schema={"type": "object"}),
        LLMPass("extract", "s", "{content}", target="entities", schema={"type": "object"}),
    ]
    client = FakeClient([
        '{"noise": true, "reason": "random characters"}',
        '{"entities": [{"category": "people", "name": "Avery"}]}',  # must never be requested
    ])
    LLMEnricher(client, passes).enrich(node, kb)
    assert len(client.calls) == 1                       # the entity pass was skipped, not called
    got = kb.get(node.id)
    assert got.entity_eligible is False and got.entity_skip_reason == "random characters"
    assert kb.promotable_entities(threshold=1) == []    # cast no votes


def test_entity_gate_keeps_a_genuine_brief_note_eligible(kb):
    # A short, low-detail note is low-signal but NOT noise -> it still participates.
    kb.upsert_entity_class("people", active=True)
    node = kb.upsert(_rec(external_id="g2", slug="g2", content="call the dentist"))
    passes = [
        LLMPass("entity_gate", "s", "{content}", target="entity_gate", schema={"type": "object"}),
        LLMPass("extract", "s", "{content}", target="entities", schema={"type": "object"}),
    ]
    client = FakeClient([
        '{"noise": false, "reason": "a real to-do"}',
        '{"entities": [{"category": "people", "name": "Avery"}]}',
    ])
    LLMEnricher(client, passes).enrich(node, kb)
    assert len(client.calls) == 2                        # the entity pass ran
    assert kb.get(node.id).entity_eligible is True
    staged = {(g["entity_type"], g["normalized_name"]) for g in kb.promotable_entities(threshold=1)}
    assert ("people", "avery") in staged


def test_persisted_noise_flag_gates_entity_passes_without_a_gate_pass(kb):
    node = kb.upsert(_rec(external_id="g3", slug="g3", content="junk"))
    kb.set_entity_eligible(node.id, False, reason="noise")
    node = kb.get(node.id)                               # reload carrying the persisted verdict
    passes = [LLMPass("extract", "s", "{content}", target="entities", schema={"type": "object"})]
    client = FakeClient(['{"entities": [{"category": "people", "name": "X"}]}'])
    LLMEnricher(client, passes).enrich(node, kb)
    assert client.calls == []                            # gated off by the stored flag alone
    assert kb.promotable_entities(threshold=1) == []


def test_entity_gate_fails_open_on_a_garbled_verdict(kb):
    node = kb.upsert(_rec(external_id="g4", slug="g4", content="real content"))
    passes = [LLMPass("entity_gate", "s", "{content}",
                      target="entity_gate", schema={"type": "object"})]
    LLMEnricher(FakeClient(["not json at all"]), passes).enrich(node, kb)
    assert kb.get(node.id).entity_eligible is True       # a parse blip never drops a real memory


def test_entity_gate_retires_stale_memory_keeping_its_influence(kb):
    cls = kb.upsert_entity_class("people")
    node = kb.upsert(_rec(external_id="g5", slug="g5", content="lunch with sam last tuesday"))
    kb.stage_entity_candidate(node.id, "people", "Sam")          # influence already cast
    kb.link_entity(node.id, cls.node.id, "belongs_to")
    node = kb.get(node.id)
    passes = [
        LLMPass("entity_gate", "s", "{content}", target="entity_gate", schema={"type": "object"}),
        LLMPass("extract", "s", "{content}", target="entities", schema={"type": "object"}),
    ]
    client = FakeClient([
        '{"noise": false, "stale": true, "reason": "a one-off lunch that has passed"}',
        '{"entities": [{"category": "people", "name": "New"}]}',  # frozen: must never run
    ])
    LLMEnricher(client, passes).enrich(node, kb)
    assert len(client.calls) == 1                                # entity pass skipped
    got = kb.get(node.id)
    assert got.lifecycle == "dead" and got.dead_reason == "a one-off lunch that has passed"
    assert kb.entities_of(node.id)                               # influence KEPT (not retracted)
    assert kb.promotable_entities(threshold=1)


def test_discover_pass_stores_the_models_description(kb):
    node = kb.upsert(NodeRecord(source="agent", external_id="m1", type="note",
                                content="c", slug="m1"))
    LLMEnricher._upsert_classes(kb, node, {"classes": [
        {"name": "widgets", "status": "new", "description": "reusable UI parts"}]})
    assert kb.get_entity_class("widgets").description == "reusable UI parts"


def test_stage_entities_boundary_records_occurred_at_and_drops_junk_role(kb):
    # Drive the extract pass's writer directly with a fake parsed reply carrying a valid
    # occurred_at + role on one entity and a junk role on another (the event/role boundary).
    node = kb.upsert(_rec(external_id="oa1", slug="oa1", content="kickoff meeting with sam"))
    parsed = {"entities": [
        {"types": ["event"], "name": "Kickoff Sync", "confidence": 0.9,
         "occurred_at": "2026-07-15T00:00:00Z", "role": "stakeholder"},
        {"types": ["person"], "name": "Sam", "confidence": 0.9, "role": "best-friend-forever"},
    ]}
    LLMEnricher._stage_entities(kb, node, parsed)
    with kb._pool.connection() as conn:
        rows = {r["normalized_name"]: r for r in conn.execute(
            "SELECT normalized_name, occurred_at, role FROM entity_candidates "
            "WHERE memory_id = %s", (node.id,)).fetchall()}
    assert rows["kickoff sync"]["occurred_at"] == datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert rows["kickoff sync"]["role"] == "stakeholder"
    assert rows["sam"]["role"] is None                       # junk role dropped to NULL


def test_upsert_classes_boundary_records_kind(kb):
    # Drive the discover pass's writer directly with a fake parsed reply carrying a kind.
    node = kb.upsert(NodeRecord(source="agent", external_id="m2", type="note",
                                content="c", slug="m2"))
    LLMEnricher._upsert_classes(kb, node, {"classes": [
        {"name": "widgets", "status": "new", "kind": "system"}]})
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT kind FROM entities e JOIN nodes n ON n.id = e.node_id "
            "WHERE n.external_id = 'class:widgets'").fetchone()
    assert row["kind"] == "system"


def test_analysis_excluded_node_skips_gate_and_entity_passes_keeps_summary(kb):
    node = kb.upsert(_rec(external_id="ax1", slug="ax1", content="real content"))
    kb.set_analysis_excluded(node.id, True)
    node = kb.get(node.id)
    passes = [
        LLMPass("entity_gate", "s", "{content}", target="entity_gate", schema={"type": "object"}),
        LLMPass("extract", "s", "{content}", target="entities", schema={"type": "object"}),
        LLMPass("summary", "s", "{content}", target="summary"),
    ]
    client = FakeClient(["the summary"])            # ONLY summary should be requested
    LLMEnricher(client, passes).enrich(node, kb)
    assert len(client.calls) == 1                   # gate + entity passes skipped
    assert kb.get(node.id).summary == "the summary"
    assert kb.promotable_entities(threshold=1) == []
    assert kb.get(node.id).entity_eligible is True  # exclusion is not 'noise' — flag untouched


def test_analysis_excluded_via_ancestor_folder_skips_entity_passes(kb):
    kb.upsert(_rec(external_id="m1", slug="note", parent_path="scratch", content="c"))
    kb.set_analysis_excluded(kb.get_by_path("scratch").id, True)
    node = kb.get_by_path("scratch.note")
    passes = [LLMPass("extract", "s", "{content}", target="entities", schema={"type": "object"})]
    client = FakeClient(['{"entities": [{"category": "people", "name": "X"}]}'])
    LLMEnricher(client, passes).enrich(node, kb)
    assert client.calls == []                       # inherited exclusion gates the entity pass
    assert kb.promotable_entities(threshold=1) == []


def test_real_config_loads_concept_prompts():
    # The repo's actual enrichment.toml + prompt files (KB_Core root is two levels up).
    root = Path(__file__).resolve().parent.parent
    passes = load_passes({"KB_ENRICHMENT_CONFIG": str(root / "enrichment.toml")})
    by_name = {p.name: p for p in passes}
    for name in ("discover_classes", "classify_memory", "extract_entities"):
        assert by_name[name].system, f"{name} system prompt is empty / unresolved"
    discover = by_name["discover_classes"].system.lower()
    assert "kind of thing" in discover or "type of thing" in discover  # types framing landed
    assert "concept" not in discover                      # old concept framing removed
    assert "bucket whose members" not in discover         # an old-prompt-only phrase; reflow-safe


def _kind_pass():
    return LLMPass("doc_kind", "kind-system", "Kinds:\n{doc_kinds}\n\n{content}",
                   target="doc_kind", schema={"type": "object"})


def test_doc_kind_pass_persists_normalized_kind(kb):
    node = kb.upsert(_rec(external_id="dkp1", slug="dkp1"))
    client = FakeClient(['{"kind": "  Meeting   Notes "}'])
    LLMEnricher(client, [_kind_pass()]).enrich(node, kb)
    assert kb.get(node.id).doc_kind == "meeting notes"


def test_doc_kind_pass_injects_kinds_in_use_support_ranked(kb):
    from kb_core.domain import NodePatch
    for i in range(2):
        n = kb.upsert(_rec(external_id=f"dkq{i}", slug=f"dkq{i}"))
        kb.update(n.id, NodePatch(doc_kind="meeting notes"))
    n = kb.upsert(_rec(external_id="dkq2", slug="dkq2"))
    kb.update(n.id, NodePatch(doc_kind="journal entry"))
    node = kb.upsert(_rec(external_id="dkp2", slug="dkp2"))
    client = FakeClient(['{"kind": "meeting notes"}'])
    LLMEnricher(client, [_kind_pass()]).enrich(node, kb)
    user_prompt = client.calls[0][1]
    assert user_prompt.index("- meeting notes") < user_prompt.index("- journal entry")


def test_doc_kind_degenerate_reply_persists_nothing(kb):
    node = kb.upsert(_rec(external_id="dkp3", slug="dkp3"))
    client = FakeClient(['{"kind": "ababababababababababab"}'])
    LLMEnricher(client, [_kind_pass()]).enrich(node, kb)
    assert kb.get(node.id).doc_kind is None


def test_doc_kind_runs_for_entity_ineligible_memory(kb):
    # Kind is orthogonal to entity eligibility: a noise-flagged memory still gets a kind.
    node = kb.upsert(_rec(external_id="dkp4", slug="dkp4"))
    kb.set_entity_eligible(node.id, False, reason="noise")
    node = kb.get(node.id)
    client = FakeClient(['{"kind": "reference"}'])
    LLMEnricher(client, [_kind_pass()]).enrich(node, kb)
    assert kb.get(node.id).doc_kind == "reference"


def test_doc_kind_unchanged_verdict_produces_no_write(kb):
    # Recrawl re-asks every cycle; a repeat classification of the SAME kind must not count as
    # a write -- no updated_at churn, no spurious NOTIFY, staleness-gate semantics preserved.
    from kb_core.domain import NodePatch
    node = kb.upsert(_rec(external_id="dkp5", slug="dkp5"))
    kb.update(node.id, NodePatch(doc_kind="meeting notes"))
    node = kb.get(node.id)
    before = node.updated_at
    client = FakeClient(['{"kind": "meeting notes"}'])
    LLMEnricher(client, [_kind_pass()]).enrich(node, kb)
    assert kb.get(node.id).updated_at == before


def test_clients_for_passes_unknown_pass_fails_loud(routing_toml):
    from kb_core.inference_routing import InferenceRouter, RoutingError
    from kb_core.ingest.llm import clients_for_passes

    router = InferenceRouter({}, path=routing_toml)
    with pytest.raises(RoutingError, match="brand_new_pass"):
        clients_for_passes(router, [LLMPass("brand_new_pass", "s", "u")])


class IdentifiedClient(FakeClient):
    """A FakeClient that also declares a provider:model identity."""
    def __init__(self, replies, provider, model):
        super().__init__(replies)
        self.provider = provider
        self.model = model


def test_enrich_reports_identities_of_passes_that_ran(kb):
    node = kb.upsert(_rec(external_id="prov1", slug="prov1"))
    default = IdentifiedClient(["gate reply"], "claude-cli", "claude-sonnet-5")
    local = IdentifiedClient(["extract reply"], "local", "gemma4:26b")
    passes = [LLMPass("entity_gate", "s", "g {content}"),
              LLMPass("extract_entities", "s", "x {content}")]

    report = LLMEnricher(
        default, passes, clients_by_pass={"extract_entities": local}).enrich(node, kb)

    assert report == {
        "passes": ["entity_gate", "extract_entities"],
        "providers": ["claude-cli:claude-sonnet-5", "local:gemma4:26b"],
    }


def test_enrich_reports_nothing_for_skipped_entity_passes(kb):
    node = kb.upsert(_rec(external_id="prov2", slug="prov2"))
    kb.set_entity_eligible(node.id, False)          # noise-excluded -> entity passes skipped
    node = kb.get(node.id)
    local = IdentifiedClient(["never used"], "local", "gemma4:26b")

    report = LLMEnricher(
        local, [LLMPass("extract_entities", "s", "x {content}", target="entities")]
    ).enrich(node, kb)

    assert report == {"passes": []}
    assert local.calls == []                         # the pass truly did not run


def test_enrich_with_unidentified_client_still_reports_the_pass(kb):
    node = kb.upsert(_rec(external_id="prov3", slug="prov3"))
    report = LLMEnricher(
        FakeClient(["r"]), [LLMPass("a", "s", "u {content}")]).enrich(node, kb)
    assert report == {"passes": ["a"]}


def test_worker_records_llm_providers_end_to_end(kb, pool):
    kb.upsert(_rec(external_id="prov4", slug="prov4"))
    kb.flag_stale_enrichment()
    client = IdentifiedClient(["fine"], "claude-cli", "claude-sonnet-5")
    EnrichmentWorker(kb, [LLMEnricher(client, [LLMPass("a", "s", "u {content}")])]).run()
    done = [r for r in kb.ingest_log() if r["event"] == "enrichment_done"]
    assert done[0]["detail"] == {"providers": ["claude-cli:claude-sonnet-5"]}
