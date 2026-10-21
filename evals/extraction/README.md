# Extraction Eval

Measures the **entity-extraction pass in isolation** (`extract_entities` in
`KB_Core/enrichment.toml`), upstream of retrieval, so a result is attributable to the model
and prompt rather than to ranking. `evals/retrieval/` asks "did the right memory come
back"; this asks "did the right anchors come out".

The shipped `gold.jsonl` (5 records) and `sample.jsonl` (10 records) are **synthetic**:
invented notes about Orchard Labs, a fictitious company. `examples/demo-vault/` is a small
invented vault in the same world.

## Record schema (`gold.jsonl`, `sample.jsonl`)

One memory per line, frozen together with its prompt context: `id`, `title`, `path`,
`location`, `content`, `doc_kind`, `content_chars`, `captured_at`, plus `entity_classes`
(the taxonomy block the prompt interpolates) and `existing_instances` (entities already
linked to the memory). Both drift as a graph grows, so they are captured once and replayed;
replaying live values would measure taxonomy drift instead of the model. `gold.jsonl` adds
`gold_anchors`: hand-written expected anchors (names, or objects with a `name`). It is
empty in the shipped file and in a fresh capture; fill it by hand, never from the pipeline
under test.

## Files

- `capture.py`: freezes a stratified sample (by doc kind and length) from your database,
  read-only. Writes to `$KB_EVAL_DATA_DIR/extraction` by default and refuses an in-repo
  `--out` unless `--force`, because captures are real notes.
- `run.py`: replays the frozen records through the served model via KB_Inference's `/chat`
  and checkpoints one line per item (resumable). Prompts are read from the live
  `enrichment.toml`, so a prompt edit is picked up on the next run.
- `judge.py`: pooled, blind adjudication of one or more runs with a pinned Claude judge
  (needs the `claude` CLI). It scores against the production rubric
  (`KB_Core/prompts/entities.system.md`), never the local model it is grading.
- `bench.sh`: runs several `model/think` arms in sequence, swapping the served model with
  `KB_Core/scripts/kb-model.sh`; see its header for options.
- `metrics.py`: pure scoring functions; `tests/` covers them and `judge.py`.

## Running against your install

From the repo root, with KB_Inference running (default `http://localhost:8001`; set
`KB_INFERENCE_TOKEN` if your server requires one):

```bash
export KB_ENRICHMENT_CONFIG=KB_Core/enrichment.toml
export KB_DATABASE_URL=postgresql://kb_app:<password>@localhost:5433/kb_mcp   # capture only
export KB_EVAL_DATA_DIR=/path/to/your/evals          # optional; default is the synthetic set

uv run --project KB_MCP python evals/extraction/capture.py [--gold-n 40] [--sample-n 200]
uv run --project KB_MCP python evals/extraction/run.py --model <served-model> \
    --run-dir evals/extraction/runs/mine/<arm-name> [--subset 20]
uv run --project KB_MCP python evals/extraction/judge.py --run-dir evals/extraction/runs/mine
uv run --project KB_MCP python -m pytest -q -p no:cacheprovider evals/extraction/tests
```

`--model` only labels the result; the server's configured model is what runs (a mismatch
is warned). Use `--subset N` (seeded random, identical across models) rather than
`--limit N` (first N, skewed by doc kind). `runs/` and `results/` are gitignored.

`judge.py` writes `scores.json` per arm: `precision` (share of emitted anchors the judge
accepted), `pooled_recall` (share of the judged-valid union across arms that this arm
found; the fair head-to-head number), `extended_recall` (also counts anchors the judge says
every arm missed; softer), `pooled_f1`, and `vs_gold` precision/recall/F1 only when
`gold_anchors` is filled. Without gold the scores rank arms against each other, not
against truth. At the default capture size, treat an F1 difference under about 0.05 as
noise.
