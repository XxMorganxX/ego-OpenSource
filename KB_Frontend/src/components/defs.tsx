import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** Terms the Retrieval page defines. A typo'd term is a compile error. */
export type DefTerm =
  | "must_recall" | "flat_set" | "package_set"
  | "mrr" | "neg_hits" | "precision" | "latency" | "searches"
  | "trace_calls" | "request" | "calls_per_request" | "weak_or_none" | "followup_rate" | "trace_errors"
  | "result_tokens" | "tokens_total" | "tokens_per_result" | "tokens_by_tool"
  | "junk_confidence" | "best_similarity" | "strategy" | "crawl_mode" | "judge"
  | "conf_none" | "conf_weak" | "conf_strong" | "conf_keyword" | "conf_scoped"
  | "reference_run" | "grid_cell";

export interface Def {
  label: string;
  short: string;
  long: string;
}

/** Copy source for tooltips AND the glossary — single source of truth.
 *  Text was reviewed against the metric definitions;
 *  keep tooltips and the glossary on this one source when wording changes. */
export const DEFS: Record<DefTerm, Def> = {
  must_recall: {
    label: "must_recall@20",
    short: "Share of required memories that appeared in the top 20.",
    long: "Of the memories that MUST come back for a question, the share that showed up in the top 20 results. The headline quality number — 0.9 would mean it finds ~90% of required answers. Read it for trend; the absolute level depends on your corpus and golden set.",
  },
  flat_set: {
    label: "flat set (SET-FLAT)",
    short: "Find-THE-note questions — one or two right answers each.",
    long: "The golden set of find-THE-note questions: each has one or two specific memories that must come back (an error string, a meeting, a decision). Recall@20 here reads as \"share of required notes found\" — 0.9 would mean ~9 in 10 lookups succeed.",
  },
  package_set: {
    label: "package set (SET-PACKAGE)",
    short: "Set-membership questions — \"everything belonging to X\". The hard set.",
    long: "The golden set of set-membership questions: \"everything that is part of X\", where the answer is a whole package of memories. Structurally harder than lookup, so expect a markedly lower score than the flat set — read this tile for trend; the absolute level depends on your corpus and golden set.",
  },
  mrr: {
    label: "MRR",
    short: "How high the first right answer ranks (1.0 = always first).",
    long: "Mean reciprocal rank of the first correct result: 1.0 means always ranked #1, 0.5 means typically ~position 2.",
  },
  neg_hits: {
    label: "neg_hits@20",
    short: "Known-wrong memories that snuck into the top 20.",
    long: "Some golden questions list memories that look topically close but are wrong. This counts how many of those got returned anyway — the real precision instrument on the flat set, where true precision is capped by tiny answer keys.",
  },
  precision: {
    label: "precision@20",
    short: "Share of returned rows that are right answers — read against its ceiling.",
    long: "Of the 20 rows returned, the share that are right answers. Most flat questions have 1–2 right answers, so the arithmetic maximum is far below 1.0 (roughly answers ÷ 20 per question) — read the number as share-of-possible, not as a raw score.",
  },
  latency: {
    label: "latency p50/p90",
    short: "Typical (p50) / near-worst (p90) search time — the whole call, embedding included.",
    long: "Server-side wall time of search calls (search_memories/search_graph only — follow-up reads are excluded from these percentiles) over the window; p50 is typical, p90 near-worst-case. Timing covers the whole tool call, including query embedding.",
  },
  searches: {
    label: "searches",
    short: "Search calls in the window; eval traffic excluded by default.",
    long: "Count of search calls in the summary window. Benchmark/eval traffic is tagged (caller='eval') and excluded from summaries unless the \"include eval traffic\" toggle is on — the old benchmark-flooding caveat no longer applies. Note: the Live-traces \"calls\" tile counts ALL traced retrieval calls (searches plus entity lookups, expands, follow-up reads); the latency and weak-or-none tiles are search-calls-only.",
  },
  trace_calls: {
    label: "calls",
    short: "All traced read calls in the window — searches, entity lookups, expands, follow-up reads, and the plain list/get browsers.",
    long: "Count of every traced MCP read call in the summary window: the retrieval-shaped tools (search_memories, search_graph, find_entity, expand, entity_activity, get_memory) plus the plain read tools (get_entity, concept, list_concepts, list_doc_kinds, list_memories, list_merge_suggestions), which are traced for payload cost only. Eval traffic (caller='eval') is excluded unless the \"include eval traffic\" toggle is on. The latency and weak-or-none tiles are search-calls-only — this tile is the only all-tool count.",
  },
  request: {
    label: "requests",
    short: "User requests in the window — each groups the calls one agent made to answer it.",
    long: "Consecutive calls in one MCP session no more than the gap shown on the requests tile apart (measured from one call's end to the next call's start) are one request. The grouping is assigned when the call is written and only changes if the gap is retuned and history is rebuilt with kb-turns-rebuild. One request can be dozens of calls.",
  },
  calls_per_request: {
    label: "calls / request",
    short: "Median number of calls one request took.",
    long: "Median over the window's requests of how many traced calls each one made. Rising means the agent needs more round trips to answer — more searching, more follow-up reads, or both. The sub-label carries the median tokens per request.",
  },
  weak_or_none: {
    label: "weak or none",
    short: "Share of search calls whose confidence came back weak or none.",
    long: "Of the window's search calls, the share whose confidence level was \"weak\" (related at best) or \"none\" (nothing matched). This is the KB grading its own answers — a rising rate means more questions the corpus can't really answer. Search calls only; follow-up reads carry no confidence. A window that spans a confidence recalibration mixes two calibrations.",
  },
  followup_rate: {
    label: "follow-up rate",
    short: "Requests that went on to open a result a search returned.",
    long: "Of the requests containing a search, the share where a later call in the SAME request fetched one of the ids that search returned (a get_memory or expand) — the implicit did-it-help signal, no labeling required. Measured per request; readings recorded before request grouping existed were per session and are not comparable.",
  },
  trace_errors: {
    label: "errors",
    short: "Traced calls that raised instead of answering.",
    long: "Count of traced read calls that raised an error (every traced tool, whole window). Failed calls are recorded like successes — the error text is on the trace row's drill-down.",
  },
  result_tokens: {
    label: "est. tokens",
    short: "Estimated size of what a call handed back to the agent.",
    long: "Estimated tokens in the payload this call returned. The byte size is measured exactly, as the client receives it; tokens are estimated from that byte count at a fixed bytes-per-token ratio (shown on the page, next to the numbers it produces) — read it for comparison and trend, not as an exact count. Calls traced before this measurement shipped carry no byte size and show \"—\".",
  },
  tokens_total: {
    label: "tokens returned",
    short: "Estimated context spent by the KB over the window.",
    long: "Estimated total tokens the knowledge base returned across the window. Only calls carrying a measurement are counted, so the tile states its own coverage. Note that MCP puts each result on the wire twice (readable text plus a structured copy); a client that reads both spends roughly 1.8× this figure.",
  },
  tokens_per_result: {
    label: "tokens / result",
    short: "Context cost per memory returned.",
    long: "Window total tokens divided by total result rows. Searches that return nothing still cost their envelope, so they push this figure up — which is the point: it prices wasted retrievals, not just verbose ones.",
  },
  tokens_by_tool: {
    label: "tokens by tool",
    short: "Which tools spend the most context.",
    long: "Estimated tokens broken out per tool, fattest first. search_graph is expected to lead: it returns its crawl-trace disclosure block alongside its results. The `calls` column counts only calls carrying a measurement, so it can read lower than the all-tool calls tile above.",
  },
  junk_confidence: {
    label: "junk confidence",
    short: `Should junk queries score "none"? FAIL = some cleared the weak bar.`,
    long: `Nine junk probes — nonsense strings, topics absent from the KB, stopword soup — should all come back with confidence "none". The tile's caption shows the worst offender — the best-scoring junk probe — and its cosine against the weak-confidence bar it should have stayed under. FAIL means an agent could mistake junk for a weak lead and hedge-answer instead of saying the KB does not know. Known open thread: confidence needs a relative signal, not just an absolute cosine bar.`,
  },
  best_similarity: {
    label: "best_similarity",
    short: "Best cosine match in the result set — comparable across queries.",
    long: "The best cosine similarity among the returned rows. Unlike rank (which always orders *something*), this is comparable across queries — it's what the confidence level is derived from. Not comparable across a change of embedding model or query instruction: either shifts cosines (the confidence bars are re-derived to match).",
  },
  strategy: {
    label: "strategy",
    short: "hybrid = flat ranker; cluster-crawl = adds graph traversal.",
    long: `\`hybrid\` is the measured flat ranker. \`cluster-crawl\` layers concept/entity traversal on top, which can admit rows the flat ranker missed; structure may only ADMIT candidates — only the query may rank.`,
  },
  crawl_mode: {
    label: "crawl_mode",
    short: "deterministic = rule-based hops (the production path).",
    long: `How crawl hops were admitted. \`deterministic\` is the rule-based production path. \`agentic\` meant an LLM judged hops — that tier was removed after it measured worse recall at far higher latency.`,
  },
  judge: {
    label: "judge",
    short: `The hop-admission rule; "none" = the deterministic production judge.`,
    long: `Which admission rule said yes to crawl hops. \`none\` is the deterministic production judge (not "no judging" — the name of the rule).`,
  },
  conf_none: {
    label: "confidence: none",
    short: "Nothing matched — the KB doesn't know.",
    long: `Confidence \`none\`: nothing genuinely matched. The right consumer behavior is to say the KB does not know, not to summarize the closest rows.`,
  },
  conf_weak: {
    label: "confidence: weak",
    short: "Related at best — hedge rather than assert.",
    long: `Confidence \`weak\`: the best match is related at best. Treat results as leads, not answers.`,
  },
  conf_strong: {
    label: "confidence: strong",
    short: "A genuine semantic match.",
    long: `Confidence \`strong\`: the best cosine clears the calibrated bar for a real semantic match.`,
  },
  conf_keyword: {
    label: "confidence: keyword",
    short: "Literal term hits, no vector arm — NOT low confidence.",
    long: `Confidence \`keyword\`: literal term matches with no embedding available. Self-evidently relevant — this is not a low-confidence verdict.`,
  },
  conf_scoped: {
    label: "confidence: scoped",
    short: "A hard filter selected these rows; similarity is the wrong yardstick.",
    long: `Confidence \`scoped\`: a hard facet (kind/subtree/tags/time) selected the rows, so semantic similarity is the wrong measure of whether they answer the question.`,
  },
  reference_run: {
    label: "reference run",
    short: "One timestamped eval of the production search path — the trend series.",
    long: "A full pass of the golden questions through the production search path at a point in time. These runs form the trend lines.",
  },
  grid_cell: {
    label: "grid cell",
    short: "One benchmark configuration, averaged over its repeats.",
    long: "One cell of a benchmark grid: a question set × strategy × judge combination, aggregated over repeated runs. Deterministic cells must be byte-identical across repeats.",
  },
};

/** Curated glossary order: quality → cost → confidence → eval vocabulary. */
export const GLOSSARY_ORDER: DefTerm[] = [
  "must_recall", "flat_set", "package_set", "mrr", "neg_hits", "precision", "best_similarity",
  "latency", "searches",
  "trace_calls", "request", "calls_per_request", "weak_or_none", "followup_rate", "trace_errors",
  "result_tokens", "tokens_total", "tokens_per_result", "tokens_by_tool",
  "junk_confidence",
  "conf_none", "conf_weak", "conf_strong", "conf_keyword", "conf_scoped",
  "strategy", "crawl_mode", "judge", "reference_run", "grid_cell",
];

/**
 * Horizontal correction (px) added to the tip's -50% translate so a tip of
 * `tipWidth` centered on `centerX` stays inside the viewport with `margin`
 * clearance. A tip wider than the viewport falls back to viewport-center.
 */
export function computeTipShift(
  centerX: number,
  tipWidth: number,
  viewportWidth: number,
  margin = 8,
): number {
  const half = tipWidth / 2;
  const min = margin + half;
  const max = viewportWidth - margin - half;
  const clamped =
    min > max ? viewportWidth / 2 : Math.min(Math.max(centerX, min), max);
  return Math.round(clamped - centerX);
}

/**
 * A defined term: dotted underline, tooltip on hover and keyboard focus.
 * Visibility is state-driven so the tip can be measured after it renders
 * (useLayoutEffect fires before paint — no flicker) and clamped to the
 * viewport; pure CSS centering can't know where the edges are.
 */
export function Dt({ term, children }: { term: DefTerm; children: ReactNode }) {
  const id = useId();
  const wrapRef = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  const [shown, setShown] = useState(false);
  const [shift, setShift] = useState(0);

  useLayoutEffect(() => {
    if (!shown) return;
    const wrap = wrapRef.current?.getBoundingClientRect();
    const tip = tipRef.current?.getBoundingClientRect();
    if (!wrap || !tip) return;
    setShift(computeTipShift(wrap.left + wrap.width / 2, tip.width, window.innerWidth));
  }, [shown]);

  const show = () => setShown(true);
  const hide = () => {
    setShown(false);
    setShift(0);
  };

  return (
    <span
      ref={wrapRef}
      className="dt"
      tabIndex={0}
      aria-describedby={id}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      <span
        ref={tipRef}
        role="tooltip"
        id={id}
        className={`dt-tip${shown ? " show" : ""}`}
        style={{ transform: `translateX(calc(-50% + ${shift}px))` }}
      >
        {DEFS[term].short}
      </span>
    </span>
  );
}
