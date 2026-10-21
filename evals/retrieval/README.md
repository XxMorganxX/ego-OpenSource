# Retrieval Golden Sets

Evaluation sets and harnesses for ego-OS's retrieval surface: the MCP tools an agent
uses to find things (`search_memories`, `search_graph`, `find_entity`, `entity_activity`,
`list_concepts`, `concept`, `expand`). Each item is a request an agent could receive,
plus the memories, entities and concepts a correct retrieval must surface.

The files committed here are **synthetic stand-ins**. They describe an invented company,
Orchard Labs, and match no real database. `examples/demo-vault/` is a small invented vault
set in the same world; the shipped sets are not built to validate against it. To measure
your own install, author a real set from your own corpus (see
[Your own gold data](#your-own-gold-data)).

## Files

| file | what it is |
|---|---|
| `golden.jsonl` | flat set: what *matches a phrasing* (8 synthetic items, `ga-NNN`) |
| `golden_package.jsonl` | SET-PACKAGE set: what *belongs together* (3 synthetic items, `gp-NNN`) |
| `validate.py` | schema checks, plus live-database drift checks unless `--static` |
| `run.py` | runs each item's `tool_plan` over the live MCP server and scores it |
| `shadow*.py` | write-free harness that replays `search_memories` ranking on a read-only connection |
| `bench.py`, `bench_metrics.py`, `report.py`, `ablations.py`, `off_corpus.py` | strategy-comparison bench (hybrid vs cluster-crawl, per set) and the off-corpus confidence probes; usage is in each script's docstring |

The flat and package sets ask different questions. They are scored separately and never
pooled into one number.

## Item schema

```json
{
  "id": "ga-001",
  "archetype": "solved-problem | entity-activity | meetings | org-activity | goals | stakeholders | person-facts | concept-browse | facet",
  "difficulty": "easy | medium | hard",
  "request": "the natural-language question, phrased as a user or agent would ask it",
  "tool_plan": [{"tool": "find_entity", "args": {"name": "…"}}],
  "expected": {
    "memories": [{"id": "uuid", "path": "ltree", "title": "…", "must": true}],
    "entities": [{"id": "uuid", "name": "canonical_name"}],
    "concepts": [{"name": "…", "kind": "project"}]
  },
  "negatives": [{"id": "uuid", "why": "plausible distractor that must NOT rank"}],
  "judgment": "why these are the right answers, with evidence from the data",
  "notes": "phrasing intent, e.g. zero keyword overlap to exercise the vector arm"
}
```

- **Memory ids are stable** (uuid5 of `source:external_id`, so they survive moves and
  re-syncs). `path` and `title` are kept for readability and drift detection.
- **Concepts are referenced by `(name, kind)`, not id.** The concept layer is recomputed
  by consolidation and ids churn; resolve names against the active catalog.
- `expected.memories[].must`: `true` means a correct system must surface it (the default
  when omitted); `false` means acceptable or bonus.
- `negatives[].id` may be a memory, entity or concept id (`"space": "concept"` marks the
  non-node ones).
- `tool_plan` is the expected route. A runner scoring pure retrieval may ignore it.
- Solved-problem requests are paraphrased with minimal keyword overlap to exercise the
  vector arm. Time-scoped requests use absolute windows; pin "now" if you rewrite them.

**Provenance rule for a real set:** build gold labels by inspecting the database directly
(SQL over your notes), never by running the retrieval under test. Every referenced id must
exist when you author the set.

## SET-PACKAGE (`golden_package.jsonl`)

Same schema, with archetypes disjoint from the flat set's (`PACKAGE_ARCHETYPES` in
`validate.py`), so a mis-filed item fails in either file: `aboutness` (which notes belong
to this collection), `current-state` (where X stands now; retired members are the trap),
`entity-package` (the whole file on one entity, retired notes included) and `relational`
(things joined by something the request does not name).

Two extra required fields:

- `"exhaustive": true`: every live note that belongs in the package is labelled. Precision
  is only meaningful because of this, so an item without it is rejected.
- `"provenance": {"method": "psql", "concepts_consulted": false, "notes": "…"}`: gold is
  built from note content, never copied from the concept catalog (that would grade
  consolidation, not retrieval).

Rules checked by `--against` (live only): at least 30% of distinct `must` ids sit outside
every active concept; at least one item includes a retired note; no item id or normalised
request text is shared with the flat set. An item whose `must` set equals an active
concept's membership gets a warning. Author package items as `medium` or `hard`: with
exhaustive labels and scored precision, none of them is a simple lookup.

## Validate the shipped synthetic sets

Structure only, no database. This is exactly what CI runs:

```bash
cd KB_MCP
uv run python ../evals/retrieval/validate.py ../evals/retrieval/golden.jsonl --static
uv run python ../evals/retrieval/validate.py ../evals/retrieval/golden_package.jsonl --static
uv run python ../evals/retrieval/validate.py ../evals/mcp_surface/golden.jsonl --static
```

`--static` checks duplicate ids, required fields, archetypes, the package fields, and the
`mcp_surface` id format and per-item negatives. It skips id existence, name/path/doc_kind
drift, the `mcp_surface` coverage floors and `--against`. Drop it to run the full checks
against a live database.

Pure-function tests (no database, no network), from the repo root:
`uv run --project KB_MCP python -m pytest -q -p no:cacheprovider evals/retrieval/tests`.

## Your own gold data

Every harness reads its gold from `KB_EVAL_DATA_DIR`. When it is unset, most fall back to
the shipped synthetic files; the shadow harness refuses to run. Keep real sets outside the
repo: they contain your notes' titles, paths and ids.

```
$KB_EVAL_DATA_DIR/
  retrieval/golden.jsonl          # run.py, validate.py (no-arg default), bench, shadow
  retrieval/golden_package.jsonl  # bench, ablations
  retrieval/traffic_gold.jsonl    # shadow harness only (optional)
  mcp_surface/golden.jsonl        # validate.py picks the mcp_surface rules by this dir name
  extraction/gold.jsonl, extraction/sample.jsonl
```

Set `KB_DATABASE_URL` explicitly to your **app-role** DSN (the role your services use, e.g.
`postgresql://kb_app:<password>@localhost:5433/kb_mcp`). Never use the bootstrap superuser:
`run.py` and the shadow harness fall back to a role named `kb` when the variable is unset.

## Run against your install

`run.py` drives the local, unauthenticated kb-mcp HTTP listener (`KB_MCP_TRANSPORT=http`,
default `http://127.0.0.1:3000/mcp`). It validates the set first and refuses to score on
errors (`--skip-validate` overrides). From the repo root:

```bash
export KB_DATABASE_URL=postgresql://kb_app:<password>@localhost:5433/kb_mcp
export KB_EVAL_DATA_DIR=/path/to/your/evals
uv run --project KB_MCP python evals/retrieval/validate.py      # live checks on your set
uv run --project KB_MCP python evals/retrieval/run.py [--url URL] [--k 20] [--items ga-001,ga-002]
```

Metrics per item: `must_recall@k`, `all_recall@k` (must + may), `first_hit_rank`,
`entity_recall`, `concept_recall` (goals items also require `goal.current = true`) and
`neg_hits@k` (distractors that ranked; want 0). Searches bump `nodes.access_count`, which
feeds ranking, so `run.py` snapshots and restores access state (`--no-restore` skips this)
and tags its trace rows `caller = 'eval'`. Results go to `evals/retrieval/results/`
(gitignored; `--out` to change).

## Shadow harness (write-free)

`shadow.py` replays the production `search_memories` ranking on a read-only connection
(`default_transaction_read_only=on` is always appended, and `assert_readonly()` must see a
refused UPDATE first; use a SELECT-only role for a privilege-level guarantee). PROD is the
engine's own `KnowledgeBase.search` with its access write stubbed out; REPLICA restates the
same SQL arm by arm so extra knobs can be turned (`KNOB_DEFAULTS` in `shadow.py`).

```bash
P="uv run --project KB_MCP python"
$P evals/retrieval/shadow_fidelity.py            # 0 = replica == prod, 1 = mismatch, 2 = could not run
$P evals/retrieval/shadow_bench.py --list         # variant registry
$P evals/retrieval/shadow_bench.py --sets authored,traffic --variants baseline,p1 [--tier adjudicated]
$P evals/retrieval/shadow_oracle.py --set authored --item ga-001   # exact rank of each gold id per source
$P evals/retrieval/shadow_confidence.py [--tier adjudicated]       # derive the confidence pair
```

Environment: `KB_DATABASE_URL` (read-only is forced), `KB_EVAL_DATA_DIR` (required; the
gold loaders exit when it is unset), `KB_INFERENCE_URL` (embedding service; its `/health`
model keys the query-vector cache), `KB_POLICY_CONFIG` (defaults to `KB_Core/policy.toml`
when present) and `KB_SHADOW_TRUST_CACHE_MODEL=1` (proceed with the embedding service down,
trusting the cache's model name).

`traffic_gold.jsonl` is optional (`--sets authored` skips it): real `search_memories` calls
replayed as of their own timestamp, one per line with `id`, `trace_id`, `at`, `query`,
`args`, `tier` (`adjudicated` | `single-annotator`) and `expected.memories[]` carrying
`grade` and `group`. A hit on any member of a gold group credits the group once.

Query vectors and BM25 document frequencies are cached in `evals/retrieval/.cache/shadow/`
(gitignored). On a fresh clone the CLIs count the embeddings they need first and exit 2
with the number if it exceeds `--max-embeds`; rerun with a larger value.
`--refresh-df` rebuilds the DF snapshot. Results go to `evals/retrieval/results/shadow/`.

**Confidence semantics.** `search_memories` returns `{"confidence", "results"}`; the
level is `strong`, `weak` or `none` from the top-1 similarity against `CONFIDENCE_STRONG`
and `CONFIDENCE_WEAK` in `kb_core.knowledge_base`. `shadow_confidence.py` re-derives the
pair for your corpus: WEAK = worst off-corpus junk top-1 across both search modes + 0.003
(rounded up to 3 dp); STRONG = 0.904 × the lower of the two modes' gold top-1 medians. It
passes when every junk probe reads `none` and at most 2 gold items per mode do.

**Caveats.** Deltas of ±0.01 are noise (concurrent searches move the access term). Replica
latency is relative cost only. Each results file records the git head, a hash of any
uncommitted engine diff, the gold and harness hashes, the policy file, the database, the DF
snapshot and the embedding model; compare runs only when these match.
