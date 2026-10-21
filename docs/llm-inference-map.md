# LLM Inference Map

> **Keep this current.** Every place the system runs a local LLM inference / analysis call
> is listed here. When you **add, move, or remove** such a call, update this map in the
> *same* change — record the **`file` + enclosing symbol**, the **input-context variable**
> (the variable that holds the prompt/text fed to the model), and the prompt/config source.
>
> **Anchoring: symbol names, never line numbers.** Rows anchor on a file and its enclosing
> function or method, because line numbers go stale with any edit above the call site. To
> locate a row: `grep -n "def <symbol>" <file>`.
>
> Local inference runs through **KB_Inference** (FastAPI over Ollama/Gemma/Qwen); Claude
> completion routes run through the CLI or Messages API clients in **KB_Core**. KB_Core
> orchestrates all analysis.

## KB_Core — analysis / orchestration

The "analysis" callers. Each pass's prompt is a template in `enrichment.toml`; the engine
call is the bridge in `ingest/inference.py`.

| Call site | What it does | Input-context variable | Prompt / config source |
|---|---|---|---|
| `src/kb_core/ingest/llm.py` — `LLMEnricher.enrich` (model call via `pass_client.complete` inside its per-pass loop) | runs each configured enrichment pass over a node. Every pass is selected through `clients_by_pass`, resolved per-task from `inference.toml` (`clients_for_passes`; currently the entity/classification passes route claude-cli on Haiku and `doc_kind` routes local). Same dispatcher, no new call site: `extract_entities` now also emits optional `occurred_at`/`role` per anchor; `discover_classes` now also emits a structural `kind` per class | **`context`** — dict assembled at the top of `LLMEnricher.enrich`; rendered into the user prompt via `render_template(p.user, context)` | `enrichment.toml` (`[[pass]]` system + user templates, including `prompts/doc_kind.*.md` + `schemas/doc_kind.json`, `prompts/entities.system.md` + `schemas/entities.json`, `prompts/discover_classes.system.md` + `schemas/entity_classes.json`); pass routing in ingest/reanalysis daemons; prompt text resolved by PromptResolver in load_passes — overlay from policy.toml [prompts] overlay_dir (default prompts.local/), {{include:…}} partials, {{taxonomy}} from [taxonomy] |
| `src/kb_core/analysis/reanalysis.py` — `ReanalysisWorker._process` (dispatcher call via `enricher.enrich(...)` inside its per-node loop) | re-runs the enrichers over nodes whose **inputs** moved — per-node dependency cursors, not a corpus-wide watermark. Each pass runs only when the one vocabulary it depends on changed (`extract_entities` ← active entity classes, `doc_kind` ← in-use doc kinds) or, for `entity_gate`, when its 30-day clock fires. `only=` carries that subset into `LLMEnricher.enrich`. On `ProviderUnavailable` the node is left untouched (still due), the provider is paused and the batch stops; `run`/`run_node`/`backfill` skip while a gated provider (the recrawl passes' remote providers) is paused | **`context`** (`ingest/llm.py`'s `LLMEnricher.enrich`) — same dict, rebuilt inside `LLMEnricher.enrich` per node | `enrichment.toml` |
| `src/kb_core/ingest/inference.py` — `HTTPInferenceClient.complete` | one stateless turn through KB_Inference `/chat`; handles whichever tasks `inference.toml` routes local (currently `doc_kind`, `describe_entity`, `merge_judge`). A server that cannot be reached at all (refused / no route / no host: the request never left) raises `ProviderUnavailable(provider="local")` with a 60 s `until` (`_transport_error`), so callers defer and pause instead of failing the node. So does an HTTP **503** (KB_Inference up, Ollama down or busy), with `until` from the response's `Retry-After` clamped to 5 s–15 min (`_retry_after`, default 60 s); timeouts, resets and every other HTTP error stay `InferenceError` | **`system_prompt`, `user_prompt`** params → assembled into `body` inside `HTTPInferenceClient.complete` | caller-supplied; local chat model is whatever KB_Inference serves (`OLLAMA_MODEL`) |
| `src/kb_core/ingest/claude_cli_client.py` — `ClaudeCliInferenceClient.complete` | one stateless completion via `claude -p`; handles configured Claude enrichment/judgment work and, explicitly, concept coherence + labeling; optionally `--output-format json`, which returns the envelope's `result` and stashes the call's token usage + cost on `last_usage` for the retrieval disclosure surface (off by default). Hardened by default (`--tools ""`, empty strict MCP config, no setting sources); `hardened=False` opts out | **`system_prompt`, `user_prompt`** params → `--system-prompt` + stdin; `schema` appended to system | caller-supplied; `KB_CLAUDE_CLI_MODEL` for general work, `KB_CONCEPT_LABEL_MODEL` for concepts (both default `claude-sonnet-5`) |
| `src/kb_core/ingest/anthropic_client.py` — `AnthropicInferenceClient.complete` | one stateless turn via Claude `messages.create`; optional direct-API completion route (no embeddings), same `InferenceClient.complete` contract | **`system_prompt`, `user_prompt`** params → `system=` + `messages=[{user}]`; `schema` appended to `system` | caller-supplied; `KB_ANTHROPIC_MODEL` or concept-specific model (default `claude-sonnet-5`) |
| `src/kb_core/knowledge_base.py` — `describe_missing_entities` | background describe step: drafts a one-line description for each active entity with a blank one (instances are born blank; classes as backstop), then marks it embed-dirty. A per-entity error skips that entity; a `ProviderUnavailable` stops the sweep and propagates, and `entity_resolve.run_once` pauses that provider and carries on with the local re-embed/scan/judge. Describe is skipped while its own provider is paused | **`view`** — per-entity dict (name/aliases/mention snippets) from `_entity_judge_view`, rendered by `build_describe_prompt` | `describe.py` (`build_describe_prompt` + `DESCRIBE_SCHEMA`) |
| `src/kb_core/knowledge_base.py` — `judge_and_stage_candidates` | merge judge: one "same entity?" call per candidate pair (embedding-KNN arm + lexical name arm); stages confirmed pairs, blocks rejected ones, `via` provenance prefixed into the stored reason | **`left`/`right`/`evidence`** — per-entity views from `_entity_judge_view` plus pair graph evidence from `_pair_judge_evidence` (co-mention count = counter-evidence for people/orgs; shared co-entity count), rendered by `build_judge_prompt` | `merge_judge.py` (`build_judge_prompt` + `JUDGE_SCHEMA`; system prompt carries the pair-evidence rules and a version-of/component-of/org-vs-club relation taxonomy) |
| `src/kb_core/ingest/promotion.py` — `EntityPromotionWorker._is_meaningful` | meaningfulness gate: one "keep or code symbol?" call per NEW promotable instance group; blocks judged code symbols (`entity_instance_blocks`) so they never promote. Fail-open, except `ProviderUnavailable`: the sweep pauses the provider and leaves new names unsettled, and skips the judge while that provider is paused; skipped when no client / `KB_INSTANCE_MEANING_GATE=off` | **`usr_msg`** — from `build_meaning_prompt(view["name"], entity_type, view["mentions"])`, where `view` = `candidate_judge_view(...)` | `prompts/meaning_judge.system.md` (+ partial `meaning_judge.examples.md`; overlay from `policy.toml [prompts] overlay_dir`, default `prompts.local/`) read via `KnowledgeBase.prompt`; `meaning_judge.py` (`build_meaning_prompt` + `MEANING_SCHEMA`) |
| `src/kb_core/denoise_instances.py` — `select_noise` (CLI `kb-denoise-instances`) | one-time backfill: same meaningfulness judge over each EXISTING active instance whose name clears the deterministic gate; flags code symbols for reversible demotion. Fail-open (inference error → skip). Only the CLI process | **`usr_msg`** — from `build_meaning_prompt(view["name"], "", view["mentions"])`, where `view` = `_entity_judge_view(node_id)` | `prompts/meaning_judge.system.md` (+ partial `meaning_judge.examples.md`; overlay from `policy.toml [prompts] overlay_dir`, default `prompts.local/`) read via `KnowledgeBase.prompt`; `meaning_judge.py` (`build_meaning_prompt` + `MEANING_SCHEMA`) |
| `src/kb_core/knowledge_base.py` — `reembed_dirty_entities` | re-embeds entities whose name/aliases/description/mentions changed (embed_dirty), batched | **`texts`** — per-entity embed text from `build_embed_text(_entity_embed_inputs(…))` | entity fields (no prompt template) |
| `src/kb_core/ingest/embedding.py` — `EmbeddingEnricher.enrich` | embeds a node's summary-or-content (title-prefixed) and stores the vector; skips when the embed-text hash is unchanged. **Two embed calls, not one**: the first `embed` call (whole-node vector), then the second (per-chunk vectors) (`node_chunks`, only when the text is long enough to span) | **`base`** (summary if present else content) → `_embed_text(title, base)` for the whole-node vector; **`text[s:e]`** per span for the per-chunk vectors | node fields (no prompt template) |
| `src/kb_core/ingest/inference.py` — `HTTPInferenceClient.embed` | POSTs to KB_Inference `/embed`; the bridge all local memory/entity/concept embedding goes through. Same failure split as `complete`: unreachable or HTTP 503 → `ProviderUnavailable(provider="local")`, anything else → `InferenceError`; the ingest enrich lane gates on a `local` pause (`build_jobs`) | **`texts`** → request body `{"input": texts}` | caller-supplied (the embed-text above) |
| `src/kb_core/consolidate.py` — `_label` (concept labeler) | one pinned-Claude "what is this cluster?" call per discovered community: names/kinds/describes it or discards it as incoherent. Fail-open to a placeholder label built from the strongest entity names, except `ProviderUnavailable`, which aborts the pass before any write, pauses the provider and is re-raised (`run_once`'s `_pause_on_outage`). Same route in CLI and daemon | **`usr_msg`** — from `build_concept_prompt(view["entities"], view["memories"], previous_name, notes=<curation-constraint notes>)`, where `view` = `concept_member_view(...)` | `prompts/concept_judge.system.md` (overlay per `policy.toml [prompts]`) read via `KnowledgeBase.prompt`; `concept_judge.py` (`build_concept_prompt` + `CONCEPT_SCHEMA`); client from `build_consolidation_clients`, resolved via `inference.toml` `[task.concept_label]` |
| `src/kb_core/consolidate.py` — `_embed_labels` | embeds each kept concept's name+description through a separate local embedding client; best-effort, failure preserves a carried vector or leaves a new concept NULL | **`texts`** — `f"{name}\n{description}"` per kept concept | concept fields; local Qwen via KB_Inference `/embed` |
| `dashboard/api/app.py` — `curate_parse` (NL concept curation) | one call per command-box submission: compiles a human curation instruction into structured ops grounded in a server-built roster; FAIL-CLOSED (503 on inference error, out-of-roster references demoted to warnings); nothing written until /apply | **`usr_msg`** — from `build_curation_prompt(text, roster)`, roster = `concept_curation_roster(text)` | `concept_curation.py` (`build_curation_prompt` + `CURATION_SCHEMA`) |
| `KB_MCP/src/kb_mcp/server.py` — `make_query_embedders` (query_embedder), built by `embedders_from_env` for `main()` | the instructed retrieval vector for `search_memories` (one text) and `search_graph` (`[instructed, bare]`, one `/embed` batch); fail-soft to keyword-only; a read-path call | **`text`** → `instruct_query(text)[:6000]` (plus `text[:6000]` when a label vector is wanted) | `kb_core.embedding.QUERY_INSTRUCTION` (`instructed-v1`); host from `KB_INFERENCE_URL` |
| `KB_MCP/src/kb_mcp/server.py` — `make_query_embedders` (embedder) | the bare vector for `find_entity` | **`text`** → `text[:6000]` | no prompt template |
| `KB_Core/scripts/smoke_entity_embed.py` — `main` (manual smoke test) | the one check that exercises the REAL embed path end to end (every automated test mocks the client): KB_Core → `/embed` → a persisted `entities.embedding` vector. Hand-run against a live stack only; no daemon or CLI reaches it | **the literal probe string** → `HTTPInferenceClient.embed([...])` | no prompt template — a raw `/embed` call |
| `src/kb_core/ingest/agent_queue_client.py` — `AgentQueueClient.complete` | one stateless completion handed to the TENANT's own machine: inserts an `llm_jobs` row and waits for the tenant's kb-agent to post the reply (hybrid tenancy). No model runs on the host for provider `agent` | **`system_prompt`, `user_prompt`** params → `llm_jobs.system_prompt` / `user_prompt`; `schema` → `llm_jobs.schema` | caller-supplied; routed by `inference.tenant.toml` (every completion task → `agent`); the client's timeout (profile `[provider.agent] timeout`, overridden by `KB_AGENT_JOB_TIMEOUT`) is stored as `llm_jobs.deadline` |
| `KB_Agent/src/kb_agent/jobs.py` — `JobRunner.run_one` → `ClaudeCli.complete` | executes one claimed `llm_jobs` row ON THE TENANT'S MAC through kb_agent's own `claude -p` subprocess (hardened: no tools, no MCP, no settings; API-key env stripped) on the tenant's own login, then posts the raw reply back to the gateway; the server's parser judges it (hybrid tenancy). A copy of `ClaudeCliInferenceClient`'s framing, not an import (kb_agent imports only the stamp module from kb_core); a test pins argv + stdin byte-identical to it | **`job["system_prompt"]`** (+ `job["schema"]`, via `system_text`) → `--system-prompt`; **`job["user_prompt"]`** (note text) → stdin only; model = `job["model_hint"]`, else config `claude_model`, else `DEFAULT_MODEL` | server-built prompts via `AgentQueueClient`; the agent adds only the schema instruction, same text as kb_core. Run timeout = min(`JOB_TIMEOUT_S` 300 s, the claim's `deadline_in_s` − 10 s); under 15 s left the job is failed fast and claude never runs |

**Wiring (where the enricher is built, no direct model call):**
**Which tier answers each task lives ONLY in `KB_Core/inference.toml`**,
resolved by `kb_core.inference_routing.InferenceRouter` (registration table
`inference_routing.TASKS`; fail-loud both directions — a missing or unknown `[task.*]`
entry stops the daemons at start). Routing env vars are retired with startup warnings:
`KB_INFERENCE_PROVIDER`, `KB_CONCEPT_LABEL_PROVIDER`/`_MODEL`, `KB_ANTHROPIC_MODEL`,
`KB_CLAUDE_CLI_MODEL`.
`src/kb_core/ingest/daemon.py`'s `_build_enrichers` (ingestion phase) and
`src/kb_core/analysis/daemon.py`'s `main` (re-analysis daemon) construct
`LLMEnricher(None, passes, clients_by_pass=clients_for_passes(router, passes))`
(`src/kb_core/ingest/llm.py`). Current routing (as shipped in `inference.toml`): extract_entities,
entity_gate, discover_classes, classify_memory, meaning_gate, curate_parse →
claude-cli on `claude-haiku-4-5-20251001` (the `[provider.claude-cli]` default);
concept_label → claude-cli on `claude-sonnet-5` (per-task override); doc_kind,
describe_entity, merge_judge and ALL embeddings → local (the `embed` task is enforced
`must_be_local`). To drain a large backlog, doc_kind/describe_entity can be routed to
claude-cli temporarily (single-flight local Gemma serializes a wide enrichment pool);
route them back to local once the backlog is gone.
The three `knowledge_base.py` entity rows (describe / merge judge / re-embed) run in
`kb-mcp-entity-resolved` (`src/kb_core/entity_resolve_daemon.py`) via
`run_once(kb, describe_client=…, judge_client=…, embed_client=…)` — each role resolved
independently (`[task.describe_entity]` / `[task.merge_judge]` / `[task.embed]`), ordered
describe → re-embed → judge so a fresh description is embedded the same tick.
The meaningfulness gate (`EntityPromotionWorker._is_meaningful`) runs in the **ingestion**
daemon's promotion sweep: `build_jobs` resolves `[task.meaning_gate]`, gated by
`KB_INSTANCE_MEANING_GATE` (default on; `off` → no client → gate skipped, promote-all as
before).
The two `consolidate.py` rows run with clients from `build_consolidation_clients(env)` in
both `kb-consolidate` and `kb-mcp-consolidated` — the name survives as a test seam but is
now `(router.client_for("concept_label"), router.embedder())`. A failed label falls back
to a placeholder; a failed embed preserves a carried vector or leaves a new concept NULL
for a later run.
The dashboard API gains its first inference dependency here: `curate_parse`
(`dashboard/api/app.py`) builds a client via `build_inference_client(env)` **lazily, on
demand**, inside the `POST /api/concepts/curate/parse` handler — one client per request,
not built at app startup. Unlike every other row above, this path is fail-CLOSED: an
inference error returns a 503 and writes nothing (curation is a write path); the
companion `POST /api/concepts/curate/apply` endpoint never calls inference at all — it
only dispatches already-structured ops.

## KB_Retrieval — the equivalence proof (its one-time read-path completion call is gone)

Deliberately IN scope, not exempted: the policy's trigger list names a `complete`/`chat`/
`embed` call explicitly, and both rows below are real calls, not mocks.

**This section once held the system's only inference completion call on a READ path** — the
crawl's LLM hop judge, since deleted after a benchmark showed it losing recall to the
deterministic judge at far higher latency (retest trigger and resurrection path:
`KB_Retrieval/src/kb_retrieval/negatives.py`'s `agentic_hop_judge` entry). What
remains here are two `/embed` calls in service of the equivalence proof and the benchmark's
knob sweeps — no completion call, no prompt template, the same shape as every other embed row
in this file.

| Call site | What it does | Input-context variable | Prompt / config source |
|---|---|---|---|
| `KB_Retrieval/scripts/prove_equivalence.py` — `main` | embeds each golden query so the live proof compares `kb.search()` and `HybridStrategy.search()` under an actual hybrid (FTS+kNN) query, not just the FTS-only path | **`instruct_query(text)[:6000]`** — `text` = `item["request"]` from `evals/retrieval/golden.jsonl` → `HTTPInferenceClient.embed([...])` | `kb_core.embedding.QUERY_INSTRUCTION` |
| `evals/retrieval/ablations.py` — `main` | the 2×2 benchmark's in-process knob sweeps (`flat_reserve`, `bridge_fanout`, `ablate`): embeds each distinct golden item (SET-FLAT + SET-PACKAGE) ONCE, cached in `vectors` by item id and reused across every sweep config, so `ClusterCrawlStrategy.search()` runs under a real query vector rather than the degraded no-embedding path | **`instruct_query(text)[:6000]`** (plus the bare `text`, for the label vector) — `text` = `it["request"]` from `evals/retrieval/golden.jsonl` / `golden_package.jsonl` → `HTTPInferenceClient.embed([...])` | `kb_core.embedding.QUERY_INSTRUCTION` |
| `evals/retrieval/shadow.py` — `Embedder.__call__` | the write-free shadow replica's query vectors (cached) | **`text`** → `(prefix.format(q=text) if prefix else text)[:6000]` | `QUERY_INSTRUCTION` by default, or `BARE` (`BARE` embeds the raw text) |

## evals/extraction — the extraction-quality harness

Two call sites, on opposite tiers: the harness replays the corpus through the **local** model
under test, then adjudicates the results with **Claude**. Both are real completion calls, so
both are in scope; neither runs in production, and neither writes to the graph.

| Call site | What it does | Input-context variable | Prompt / config source |
|---|---|---|---|
| `evals/extraction/run.py` — `call` | one stateless `/chat` turn per frozen corpus record, replaying the production `extract_entities` pass through whichever model KB_Inference is serving. Structured output is forced with the production schema; the session is purged after every turn so no history leaks between records | **`user`** — `render_template(p.user, rec)` in `main`, where `p` is the live `extract_entities` pass and `rec` a frozen corpus record; passed to `call` as `user` and posted as `body["message"]` | prompts read LIVE from `KB_Core/enrichment.toml` (`prompts/entities.system.md` + `schemas/entities.json`) — deliberately not copied, so a prompt edit is measured rather than missed. Model comes from the server, not the harness |
| `evals/extraction/judge.py` — `main` | pooled adjudication: one call per corpus record, grading the shuffled union of every arm's anchors as valid/invalid and proposing anchors all arms missed. Pinned to Claude so the judge is never the model under test; `hardened=True` because the memory text is untrusted corpus content reaching a tool-bearing subprocess | **`system`, `user`** — from `build_prompts(record, candidates, rubric)`, where `candidates` = `pool_candidates(...)` and `rubric` is read from the production entities prompt | `judge.py` (`JUDGE_SYSTEM` + `JUDGE_SCHEMA`), with the rubric embedded live from `KB_Core/prompts/entities.system.md`; model from `KB_EXTRACTION_JUDGE_MODEL` (default `claude-sonnet-5`) |

## KB_Inference — engine (raw Ollama calls)

Where text actually hits the model. KB_Core never calls these directly — only over HTTP.

| Call site | What it does | Input-context variable | Prompt / config source |
|---|---|---|---|
| `conversation.py` — `ConversationManager.chat` | sync chat turn via `ChatOllama.invoke` | **`message`** (full prompt = system + history + turn, assembled in `Session.build_messages`, `conversation.py`) | per-session system prompt |
| `conversation.py` — `ConversationManager.achat` | async chat turn via `ChatOllama.ainvoke` | **`message`** (see `build_messages`, `conversation.py`) | per-session system prompt |
| `conversation.py` — `ConversationManager.astream` | streamed chat via `ChatOllama.astream` | **`message`** (see `build_messages`, `conversation.py`) | per-session system prompt |
| `embeddings.py` — `EmbeddingService.embed` | sync text→vectors via `OllamaEmbeddings.embed_documents` | **`texts`** | — (embedding model) |
| `embeddings.py` — `EmbeddingService.aembed` | async text→vectors via `OllamaEmbeddings.aembed_documents` | **`texts`** | — (embedding model) |

**Decoding settings apply to every row above.** `ConversationManager` builds one `ChatOllama`
for all three chat call sites, so `temperature`, `seed`, `keep_alive`, `num_ctx`,
`num_predict` and `reasoning` are properties of the manager, not of an individual call.
`reasoning` (env `OLLAMA_THINK`, Ollama's `think`) is **tri-state**: unset
leaves the decision to the model, which is not the same as `false`. It is read once at process
start, so changing it requires a service restart — `kb-model.sh use TAG --think on|off|default`
does the write, restart, and verification. `/health` reports the live value.

**HTTP entrypoints (thin wrappers over the above):**

| Endpoint | Handler | Input-context variable |
|---|---|---|
| `POST /chat` | `server.py` `chat` → `manager.achat` | `req.message` (+ `req.system_prompt`) |
| `POST /chat/stream` | `server.py` `chat_stream` → `manager.astream` | `req.message` |
| `POST /embed` | `server.py` `embed` → `embedder.aembed` | `req.input` → `texts` |

`chat` and `embed` answer **503 + `Retry-After`** (`KB_INFERENCE_RETRY_AFTER`, default 30 s)
when the Ollama call fails because Ollama is unreachable or answered 503
(`server.py` `upstream_unavailable` / `_unavailable`); every other model error stays a 500.
KB_Core's `HTTPInferenceClient` maps that 503 to a `local` provider pause, not a node failure.

Every endpoint sits behind `server.py`'s `ExposureGuard` (loopback Host header and client,
plus `KB_INFERENCE_TOKEN` as a bearer token when set). Callers send the token through
`kb_core.ingest.inference.inference_auth_headers` (`HTTPInferenceClient` does it on every
request). Session read-back (`GET /sessions`, `GET /sessions/{id}`) returns the prompts fed
to the model and is off unless `KB_INFERENCE_DEBUG_SESSIONS=1`.

> **`/embed` consumer:** `EmbeddingEnricher` (`ingest/embedding.py`) embeds each node during the
> enrich and recrawl phases via `HTTPInferenceClient.embed`; `kb-embed-backfill` backfills the
> existing corpus. The **read** path is also a consumer: `kb.search()` is a
> hybrid FTS+kNN ranker whose query vector KB_MCP fetches through this endpoint
> (`server.py`'s `embedder`, fail-soft to FTS-only), and the search tools send the instructed
> query (and `search_graph` also the bare one, for concept/entity routing) in one batch;
> `find_entity` sends the bare name.

## Coverage

The tables above are the complete set of local-LLM call sites. The history of how each row
was added, moved or removed lives in the git log, not here; this section keeps only what a
future sweep needs.

Reproduce the sweep (from the repo root, `grep -rinE` with `--include={*.py,*.ts,*.tsx,*.js}`,
excluding `.venv node_modules __pycache__ .git dist build backups`):

- **Model clients:** `ChatOllama|OllamaEmbeddings|ChatAnthropic|ChatOpenAI|OpenAI\(|Anthropic\(|llama_cpp|vllm|transformers|HuggingFace|SentenceTransformer|genai|cohere|mistralai|together|replicate|groq`
- **Invocations:** `\.ainvoke\(|\.invoke\(|\.astream\(|\.embed_documents|\.aembed_documents|\.embed_query|messages\.create|\.completions|\.complete\(|\.embed\(|\.aembed\(|\.achat\(`
  (`\.embed\(` matters: it is the form **every** KB_Core embed call uses; without it the
  regex cannot find most of the embed rows above.)
- **HTTP callers:** `KB_INFERENCE_URL|/chat|/embed|HTTPInferenceClient|InferenceClient`
- **New top-level directories** need sweeping too: a call site in a directory nobody sweeps
  (such as the extraction harness's `/chat` call) is easy to miss.

**Checked and confirmed NOT inference call sites** (so they aren't re-investigated each sweep):

- **KB_Frontend** — "inference" only appears in error handling (classifying `InferenceError`
  timeouts as retryable, `lib/ingest.ts`); its `/api` calls go to the **dashboard API** (`:8000`),
  not the inference server (`:8001`). No chat/embed calls.
- **Dashboards** — `KB_Core/dashboard` has one model call, the NL concept-curation endpoint `curate_parse` (see the KB_Core table above and
  its wiring paragraph) — everything else in `dashboard/api/app.py` is
  parse-args/call-core/format.
- **KB_MCP** — thin transport; holds no **completion** call site of its own (engine logic lives
  in KB_Core, retrieval policy in KB_Retrieval), and `server.py` builds no completion client.
  Its optional `/embed` query embedder is a call site and has a row above
  (`KB_MCP/src/kb_mcp/server.py` — spelled in full because a bare `server.py` is ambiguous
  with `KB_Inference/server.py`).
- **`evals/retrieval/off_corpus.py`** and **`evals/retrieval/run.py`** — they only call the
  already-mapped `search_memories`/`search_graph` MCP tools.
- **`evals/extraction/capture.py`** — reads Postgres read-only and writes JSONL; it contacts
  no model.
- **`KB_Inference/embeddings.py`'s `__main__` block** — `svc.embed(...)` inside `if __name__ == "__main__"`, a
  standalone smoke block. Not a distinct call site: it reaches the model through the already
  mapped `EmbeddingService.embed` (`embeddings.py`). Same for
  `KB_Inference/conversation.py`'s `Session` facade, a facade delegating to the mapped `achat`/`astream`.

### Notes that apply across rows

- **Entity extraction `confidence` means anchor durability**, not textual groundedness: "would
  a different note, written months from now, use this same name for this same thing?" — the
  same question `meaning_judge.py` asks, so extraction confidence is a cheap predictor of that
  second pass. Groundedness is near-constant by construction (you can only extract what the
  text contains), so it does not predict the meaning judge.
  `schemas/entities.json` keeps the 0.9/0.6/0.3 enum; `ingest/llm.py` keeps the bottom band
  and `knowledge_base.promotable_entities` withholds entity *creation* from an all-bottom-band
  group instead.
- **Enrichment concurrency.** `EnrichmentWorker.run` (`ingest/enrichment.py`) processes its
  claimed batch on a `ThreadPoolExecutor` when `KB_ENRICH_CONCURRENCY` > 1 (default **1**, the
  serial loop). Only the ACROSS-node loop is widened: passes within a memory stay ordered,
  because they are a dependency chain (`entity_gate` must be able to suppress the entity
  passes before they run, and later user templates interpolate earlier pass outputs). Two
  ceilings: a task routed `local` goes to the single-flight KB_Inference server, so the
  speedup is bounded by the local share, not linear; and `daemon.main` sizes the pool as
  `max(4, concurrency + 4)` so worker threads do not stall on `PoolTimeout`. Each claude-cli
  call is one `claude -p` subprocess, so concurrency N means N live subprocesses and N× the
  rate-limit and auth-expiry exposure.
- **Two scheduler lanes** (`run_lanes`, `ingest/scheduler.py`): enrichment + promotion (the
  LLM jobs) tick on a slow lane while source pulls and webhook drains tick on a fast lane, so
  an enrichment batch never blocks ingestion.
- **Describe/judge evidence selection.** `_entity_judge_view` (feeding describe, merge judge
  and denoise) picks its 5 mention snippets in this order: the memory titled with the
  entity's canonical name (its own page), then `entity_links.role = 'subject'`, then link
  `weight`, then recency (both boolean keys coalesced to false), and reads
  `left(content, 1200)`. The shared `_mention_lines` passes each snippet through
  `entities.mention_snippet`, which skips leading header/metadata lines such as `#` headings
  and `**Participants:**`. `candidate_judge_view` (the meaning gate) also uses that helper
  but reads `left(content, 200)` in recency order, so a long email header can shrink its
  snippet to a short body fragment, though never to empty: an all-boilerplate window falls
  back to the raw text.
- **The memory's folder path** ("Filed under", humanized by `paths.humanize_path`) is part of
  the input context of the four enrichment system prompts, the mention snippets in
  `_entity_judge_view` / `candidate_judge_view`, and `concept_member_view` memory tuples.
  Embed texts deliberately leave it out (adding it would dirty every vector).
- **The extraction eval harness** (`evals/extraction/run.py`) replays through KB_Inference, so
  it measures the local model even when `extract_entities` is routed to claude-cli in
  production — keep that in mind when reading its numbers against production.
