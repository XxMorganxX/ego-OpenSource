# KB_Retrieval

Retrieval strategies for the KB, behind one swappable contract.

```python
from kb_retrieval.contract import Query
from kb_retrieval.registry import get_strategy

result = get_strategy("hybrid", kb=kb).search(Query(text="how did we fix the stall"))
result.confidence["level"]   # none | weak | strong | keyword | scoped
result.cost.tokens_returned
```

```python
crawl = get_strategy("cluster-crawl", kb=kb).search(Query(text="…", embedding=vec))
crawl.trace.entry            # which concepts/entities routed the query
crawl.trace.stop_reason      # judge-stop | marginal-yield | pool-cap | max-hops
crawl.ignored_filters        # ["include_dead@concept-arm"] — a disclosed blind spot
```

| Strategy | Cost | What it is |
|---|---|---|
| `hybrid` | cheap | FTS + vector kNN fused by RRF. The measured default. |
| `cluster-crawl` | cheap (deterministic) | Concept/entity-routed graph crawl, **unioned** with the flat pool. `judge="none"` today — no LLM, no network. |

`KnowledgeBase.search()` (in `KB_Core`) stays as the equivalence reference for `hybrid`
until the strategy fully replaces it; `scripts/prove_equivalence.py` checks the two
against the live golden set.

`cluster-crawl` ships with `DeterministicHopJudge` (`judge="none"`) and is fully CI-testable
with **no LLM, no network, and no subprocess** — that is the point: the judge seam makes an
LLM tier an optional precision improver later, never the thing recall depends on today.
KB_MCP's `search_graph` tool exposes this strategy. An LLM hop judge was built and then
measured in a 2×2 recall/precision benchmark across both strategies; it lost recall at
far higher latency and was deleted (see `kb_retrieval/negatives.py`, entry
`agentic_hop_judge`, for the measurement and the re-test trigger).

## Install / test

```bash
uv sync                          # installs ego-os-core (KB_Core) as an editable path dependency
uv run pytest                    # needs a Postgres test DB — see the root README's Testing section
```
