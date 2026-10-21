"""The contract every retrieval system must satisfy.

A strategy declares what it can honour (`capabilities()`) and reports what it
actually did (`applied_filters` / `ignored_filters` on the result). The pair is
deliberate: the static half lets a benchmark plan a valid matrix before spending
expensive calls, and the runtime half makes it impossible to silently drop a facet
and be scored as a win for it. This mirrors the house habit of disclosure over
enforcement -- confidence.level, time_basis, undated_count, embedder: "unavailable".
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

# The filter set mode A honours through one shared SQL block, `SEARCH_FILTERS`
# (kb_core.knowledge_base.SEARCH_FILTERS). A facet must never be outflankable by a vector hit,
# so every strategy is measured against this exact set.
FILTER_NAMES = frozenset({"under", "source", "type", "tags", "kind",
                          "since", "until", "include_dead"})

# The seven filters that thread through every primitive as None-defaulted keyword
# arguments. include_dead is deliberately NOT here: it is a bool with a True
# default, so "not set" is not expressible as None, and every caller passes it
# explicitly. Both strategies build their **filters dict from THIS tuple rather than
# a hand-copied local, so a facet cannot quietly stop being passed by one of them.
FILTER_FIELDS = ("under", "source", "type", "tags", "kind", "since", "until")

# The one cost unit comparable across every strategy (see RetrievalStrategy's
# contract below). ONE formula, shared: two strategies with two independent
# ~4-chars-per-token heuristics would make the benchmark's cost column incomparable
# without ever raising. Deliberately crude -- this is a RELATIVE yardstick between
# strategies over the same corpus, not a tokenizer.
CHARS_PER_TOKEN = 4


def estimate_tokens(*parts: "str | None") -> int:
    """Approximate tokens for the text a strategy actually returns.

    Floor-divides EACH part separately rather than concatenating first. That is the
    arithmetic HybridStrategy already shipped (`len(snippet)//4 + len(title)//4`),
    and the two differ whenever a part's length is not a multiple of 4 -- changing it
    would move mode A's reported cost for no measured reason.
    """
    return sum(len(p or "") // CHARS_PER_TOKEN for p in parts)


@dataclass(frozen=True)
class Capabilities:
    filters: frozenset[str]
    needs_embedding: bool
    uses_llm: bool
    cost_class: Literal["cheap", "expensive"]


HYBRID_CAPS = Capabilities(filters=FILTER_NAMES, needs_embedding=False,
                           uses_llm=False, cost_class="cheap")

CRAWL_CAPS = Capabilities(filters=FILTER_NAMES | {"anchor_entity"},
                          needs_embedding=True, uses_llm=True,
                          cost_class="expensive")

# Mode B with the DETERMINISTIC hop judge -- the same pipeline with decide()
# replaced by a rule, so the filter set is identical and only the
# LLM-dependent facts change. Declared here rather than mutated at call time so a
# benchmark harness can name both cells statically. `uses_llm` is the planning-time
# twin of the runtime flag `cost.llm_calls == 0`; a harness sweeping only cheap
# strategies must not skip a crawl that makes zero LLM calls, because that crawl is
# half of the 2x2. cost_class is "cheap" because without the LLM the crawl is ~0.4 s
# against flat's ~150 ms -- the same order of magnitude, not the far higher
# latency the "expensive" warning exists for.
CRAWL_DETERMINISTIC_CAPS = Capabilities(
    filters=CRAWL_CAPS.filters, needs_embedding=True,
    uses_llm=False, cost_class="cheap")


@dataclass
class Query:
    """Defaults mirror the deployed search tools' parameters exactly (KB_MCP
    server.py, search_memories/search_graph), so a strategy swap cannot silently
    change what a caller asked for.

    Two query vectors: `embedding` is the RETRIEVAL vector -- the
    query embedded with kb_core.embedding.QUERY_INSTRUCTION -- read by every
    query-vs-document consumer (kNN legs, similarity_for, bridge_frontier).
    `label_embedding` is the BARE query vector for symmetric/label matching (concept
    label routing, find_entities); read it via label_vector(), which falls back to
    `embedding` for callers that supply only one.

    include_dead defaults True because filtering retired memories unconditionally
    measured a must_recall loss on historical-lookup items.
    """
    text: str
    under: str | None = None
    source: str | None = None
    type: str | None = None
    tags: list[str] | None = None
    kind: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_dead: bool = True
    limit: int = 20
    embedding: list[float] | None = None
    anchor_entity: UUID | None = None
    label_embedding: list[float] | None = None

    def label_vector(self) -> "list[float] | None":
        """The bare query vector for label/symmetric matching; `embedding` when the
        caller supplied only one vector."""
        return self.label_embedding if self.label_embedding is not None else self.embedding


@dataclass
class Row:
    """A 1:1 port of `kb_core.domain.SearchResult` -- `rank`/`score` keep that
    dataclass's exact two-branch meaning so a strategy swap
    cannot silently change what MCP puts on the wire (KB_MCP's search_memories puts
    `r.rank` straight into the response):

      rank  -- FTS-only (no query embedding supplied): the raw ts_rank relevance
                value, unchanged by fusion. hybrid (query embedding supplied): the
                fused RRF score. Which branch applies is decided the same way
                SearchResult decides it -- by whether the caller passed an
                embedding, not by whether the kNN arm actually returned anything.
      score -- the importance/retrieval-recency decayed FTS score, in EITHER
                branch. 0.0 means this row was not an FTS hit (e.g. a kNN-only row
                in the hybrid branch, mirroring the SQL's COALESCE(fused.score, 0)).

      The seven fields after `content_chars` are DISPLAY metadata, mirroring what
      KB_MCP's `_hit_dict` puts on the wire so the two search tools return the same
      information about the same note. They are defaulted, they are populated from
      the `Node` each strategy already holds, and they never enter ranking.
      `tests/test_row_display_fields.py` proves both strategies fill them from the
      node; the SUPERSET claim against `_hit_dict`'s own output keys is asserted in
      `KB_MCP/tests/test_search_graph.py`, because nothing here may read `kb_mcp`
      (the dependency direction is one-way). That test derives the required set from
      `_hit_dict`'s real output rather than a hand-copied list, so it stays true when
      that function
      grows a field.
    """
    id: UUID
    path: str
    title: str | None
    similarity: float | None
    rank: float
    score: float = 0.0
    via: list[str] = field(default_factory=list)
    snippet: str | None = None
    doc_kind: str | None = None
    event_at: datetime | None = None
    lifecycle: str | None = None
    content_chars: int = 0
    # DISPLAY FIELDS -- what an agent reads, not what a ranker reads. Added so
    # search_graph is not strictly poorer than search_memories: KB_MCP's `_hit_dict`
    # emits all seven, Row expressed none of them, and the design
    # warns that a mode B which is not compelling "becomes dead code with a
    # maintenance bill". `summary` is the load-bearing one -- _hit_dict's own
    # docstring makes it what an agent reads INSTEAD of the note body, which rows
    # deliberately omit (full bodies made one default search hundreds of KB, almost
    # all of it content).
    #
    # ALL DEFAULTED, so every existing construction site and every test fake is
    # untouched, and populated in BOTH strategies from the kb_core.domain.Node that
    # hydrate() already returned -- zero extra round trips, zero extra SQL. They take
    # no part in ranking and appear in no ORDER BY; `importance` in particular is a
    # DISPLAY value here and must never become a ranking currency (it is already
    # folded into the FTS score server-side).
    type: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    uri: str | None = None
    summary: str | None = None
    importance: float | None = None
    dead_reason: str | None = None


@dataclass
class Cost:
    tokens_returned: int = 0
    latency_ms: int = 0
    llm_calls: int = 0
    provider_tokens: int | None = None
    provider_cost_usd: float | None = None


@dataclass
class Trace:
    # Which admission tier actually decided this crawl: "agentic" (every hop that ran
    # was query-conditioned judgment), "mixed" (some hops were, some fell back or ran
    # deterministic), "deterministic" (no hop's admission was query-conditioned --
    # including a crawl that made ZERO hops at all, the common shape: the design's
    # agentic_fraction gate depends on a no-hop crawl reading as deterministic, not
    # vacuously as agentic). Derived per hop from what actually decided
    # (`hops_trace[]["agentic"]`), never from the judge's declared capability alone
    # -- see crawl/strategy.py's `hop_agentic` and `crawl_mode`.
    crawl_mode: str | None = None
    hops: list[dict] = field(default_factory=list)
    entry: list[dict] = field(default_factory=list)
    bridge_capped: list[dict] = field(default_factory=list)
    consolidated_at: datetime | None = None
    undated_count: int = 0
    time_basis: str | None = None
    flat_displaced: dict = field(default_factory=dict)
    degraded: dict = field(default_factory=dict)
    # Which stop rule ended the crawl: "judge-stop" | "marginal-yield" | "pool-cap" |
    # "max-hops" | "deadline" | None (no hop ran). The design lists four stops in
    # priority order; "deadline" is a fifth, added by the crawl-wide wall clock
    # (CRAWL_DEADLINE_MS) -- it fires from the mid-loop pre-check, BEFORE a hop even
    # builds a frontier, and is distinct from a single hop falling back on the LLM
    # deadline (that hop still runs deterministically and reports its own stop
    # reason, typically "judge-stop"). The deterministic judge always fires
    # "judge-stop" first -- which is exactly why this field must exist at all:
    # without it the other four are unobservable and therefore untestable unless a
    # judge or a clock reaches them.
    stop_reason: str | None = None
    # Indices a judge emitted outside the offered frontier, dropped and counted.
    # A judge cannot emit a node id at all, so an out-of-range index is
    # the only shape a malformed or hostile admission can take; dropping it silently
    # would hide the one signal an LLM judge needs monitored.
    rejected_admissions: int = 0
    # The design's disclosure surface. "ok" | "unavailable (quota, resets ...)" |
    # "unavailable (hourly budget 20/h exhausted)" | "deterministic (another query
    # holds the LLM slot)" | "deterministic (deadline: ...)" | "unused (no llm hop
    # ran)". None means NO LLM TIER WAS SELECTED at all -- distinct from "ok", which
    # claims a successful call, and from a degradation string, which names a failed
    # one. Reported beside cost.llm_calls, never instead of it.
    llm: "str | None" = None
    # The design's "Cold / stale concept layer" row: no concept family produced an entry
    # seed, so the flat pool and the name anchor carried the query alone. Read it
    # together with consolidated_at (None = never consolidated): an empty trace.entry
    # looks identical whether the crawl found nothing or the layer is cold, and many
    # notes may sit in no active concept, so this is the common case rather than an
    # alarm.
    stale_concepts: bool = False


@dataclass
class RetrievalResult:
    rows: list[Row]
    confidence: dict
    applied_filters: dict
    ignored_filters: list[str]
    cost: Cost
    trace: Trace
    # Per-stage replay record (kb_retrieval.replay) for the dashboard's retrieval
    # replay. Advisory: None when capture failed (trace.degraded["replay"] says why)
    # or a strategy does not capture. Never part of any tool's wire output.
    replay: "dict | None" = None


@runtime_checkable
class RetrievalStrategy(Protocol):
    """A retrieval system.

    Contract:
      - `search` MUST apply every filter named in `capabilities().filters` that the
        query sets, and MUST list any it could not honour in
        `RetrievalResult.ignored_filters` using `"<filter>@<arm>"` form.
      - `confidence` MUST use `kb_core.knowledge_base.search_confidence`'s shape
        unchanged, so strategies stay directly comparable.
      - `cost.tokens_returned` MUST be populated by every strategy -- it is the one
        cost unit comparable across all of them.
      - `search` MUST NOT raise for an empty result; an empty `rows` with
        `confidence.level == "none"` is the correct answer to a question the KB
        cannot answer.
    """
    name: str

    def capabilities(self) -> Capabilities: ...
    def search(self, q: Query) -> RetrievalResult: ...
