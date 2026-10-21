# MCP-Surface Golden Set

An eval set for whether the **right documents get collected** when an agent serves a
request through the kb-mcp tools: tool choice, query phrasing and follow-up calls, judged
by what the session retrieved. It is not a ranking benchmark (`evals/retrieval/` covers
that) and it does not grade the answer a session writes.

The committed `golden.jsonl` is a five-item **synthetic stand-in** (`ms-001`…`ms-005`)
about Orchard Labs, an invented company that exists in no real database. It is checked for
shape only. `examples/demo-vault/` is a small invented vault in the same world; the shipped
items are not built to validate against it.

## Item schema

The flat-set schema from [`evals/retrieval/README.md`](../retrieval/README.md#item-schema),
with ids of the form `ms-NNN`, at least one entry in `negatives` per item, and one optional
field: `retired_ok: true` marks an item whose must-gold deliberately includes retired
(deleted) notes. `tool_plan` is diagnostic context, never a rubric.

## Validate the shipped set

Structure only, no database (what CI runs):

```bash
cd KB_MCP
uv run python ../evals/retrieval/validate.py ../evals/mcp_surface/golden.jsonl --static
```

`validate.py` applies the mcp_surface rules to any file whose parent directory is named
`mcp_surface`. Under `--static` it checks the `ms-NNN` id format, per-item negatives and
`retired_ok`. Because a static run has no note metadata, an item with `retired_ok: true`
always fails there, so a synthetic set must not set it.

## Building a real set

Put it at `$KB_EVAL_DATA_DIR/mcp_surface/golden.jsonl` (outside the repo; it holds your
notes' ids and titles) and validate it against your database, from the repo root:

```bash
export KB_DATABASE_URL=postgresql://kb_app:<password>@localhost:5433/kb_mcp
uv run --project KB_MCP python evals/retrieval/validate.py "$KB_EVAL_DATA_DIR/mcp_surface/golden.jsonl"
```

Rules for a real set:

- **Provenance.** Build gold by inspecting the database directly, never by running the
  retrieval being evaluated. Have someone re-derive each item's answers from the request
  alone; reject items they disagree on rather than patching them.
- **Floors (enforced live).** At least 40% of items carry must-gold ingested after
  `MCP_SURFACE_CUTOFF` in `validate.py` (set it to the date your older sets were authored,
  so the set covers new content). At least 3 items set `retired_ok: true` and have retired
  must-gold; `retired_ok` on an item with no retired must-gold is an error.

## Grading contract

No session harness ships yet. A harness for this set should run each item as a headless
agent session with kb-mcp over stdio and `KB_MCP_CALLER=mcp-surface:<item-id>` in the
server's environment, so every `mcp_trace` row joins to its item. Grade from
`mcp_trace.results` unioned across the session's calls: **must-recall** (fraction of
must-gold surfaced anywhere), **negative-avoidance** (negatives that appeared) and rank
diagnostics (`results[].rank` is position, not score). Run sessions one at a time and
keep background writers (ingest, enrichment, consolidation) stopped during a run.

This set and the retrieval sets answer different questions and are never pooled.
