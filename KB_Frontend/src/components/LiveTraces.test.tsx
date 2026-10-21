import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Trace, TraceDetail, TraceSummary, Turn, TurnDetail } from "../types";

const fetchTraces = vi.fn();
const fetchTraceSummary = vi.fn();
const fetchTraceDetail = vi.fn();
const postTraceJudgment = vi.fn();
const fetchTurns = vi.fn();
const fetchTurnDetail = vi.fn();
const postTurnJudgment = vi.fn();
vi.mock("../api/client", () => ({
  fetchTraces: (...a: unknown[]) => fetchTraces(...a),
  fetchTraceSummary: (...a: unknown[]) => fetchTraceSummary(...a),
  fetchTraceDetail: (...a: unknown[]) => fetchTraceDetail(...a),
  postTraceJudgment: (...a: unknown[]) => postTraceJudgment(...a),
  fetchTurns: (...a: unknown[]) => fetchTurns(...a),
  fetchTurnDetail: (...a: unknown[]) => fetchTurnDetail(...a),
  postTurnJudgment: (...a: unknown[]) => postTurnJudgment(...a),
}));

let feedCb: ((tables: Set<string>) => void) | undefined;
vi.mock("../hooks/useChangeFeed", () => ({
  useChangeFeed: (cb: (tables: Set<string>) => void) => {
    feedCb = cb;
  },
}));

vi.mock("./ReplayOverlay", () => ({
  ReplayOverlay: ({ traceId }: { traceId: number }) => <div>replay overlay {traceId}</div>,
}));

import { LiveTraces } from "./LiveTraces";

const summary: TraceSummary = {
  days: 7, calls: 12, latency_p50_ms: 180, latency_p90_ms: 420,
  weak_or_none: 3, weak_or_none_rate: 0.25, embedder_failures: 0, errors: 1,
  llm_calls: 0, confidence_levels: { strong: 9, weak: 3 }, followup_rate: 0.5,
  turns: 44, calls_per_turn_median: 6.5, tokens_per_turn_median: 2600, turn_gap_s: 90,
  // Every number here must be textually distinct from every other rendered value,
  // including the trace row's own result_tokens below — getByText fails on a
  // multiple match, so a shared 1,200 would break the tile tests in Task 5.
  tokens_measured: 9, tokens_median: 1500, tokens_total: 41000,
  tokens_per_result: 380, bytes_per_token: 3.5,
  tokens_by_tool: [
    { tool: "search_graph", calls: 6, tokens_median: 2400, tokens_total: 33000 },
    { tool: "get_memory", calls: 3, tokens_median: 200, tokens_total: 8000 },
  ],
};

const trace: Trace = {
  id: 1, at: "2026-07-29T10:00:00+00:00", request_id: "r-1", session_id: "s-1",
  turn_id: 1,
  caller: null, tool: "search_graph", query: "why was the judge deleted",
  target_id: null, latency_ms: 190, result_count: 5,
  confidence_level: "strong", best_similarity: 0.6425,
  strategy: "cluster-crawl", judge: "none", crawl_mode: "deterministic",
  llm_calls: 0, embedder_ok: true, error: null, judgment: "good",
  result_bytes: 4200, result_tokens: 1200,
};

beforeEach(() => {
  vi.clearAllMocks();
  // The legacy suite below exercises the flat table; grouped mode is the default, so
  // pin it off here and let the grouped suite clear the key.
  window.localStorage.setItem(GROUP_KEY, "false");
  fetchTraces.mockResolvedValue({ traces: [trace] });
  fetchTraceSummary.mockResolvedValue(summary);
  fetchTurns.mockResolvedValue({ turns: [turn] });
  fetchTurnDetail.mockResolvedValue(turnDetail);
});

describe("LiveTraces", () => {
  it("renders tiles, follow-up rate, and the trace table", async () => {
    render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByText("why was the judge deleted")).toBeInTheDocument());
    expect(screen.getByText("50%")).toBeInTheDocument();       // follow-up tile
    expect(screen.getByText("180 ms")).toBeInTheDocument();    // p50 tile
    expect(screen.getByText("good")).toBeInTheDocument();      // judgment chip
  });

  it("renders an explicit empty state", async () => {
    fetchTraces.mockResolvedValue({ traces: [] });
    render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByText(/No retrieval calls traced yet/)).toBeInTheDocument());
  });

  it("shows an error state when the initial fetch fails", async () => {
    fetchTraces.mockRejectedValue(new Error("boom"));
    render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByText(/failed to load traces/)).toBeInTheDocument());
  });

  it("refetches on mcp_trace change-feed events only", async () => {
    render(<LiveTraces isOwner />);
    await waitFor(() => expect(fetchTraces).toHaveBeenCalledTimes(1));
    feedCb?.(new Set(["nodes"]));
    expect(fetchTraces).toHaveBeenCalledTimes(1);
    feedCb?.(new Set(["mcp_trace"]));
    await waitFor(() => expect(fetchTraces).toHaveBeenCalledTimes(2));
  });

  it("passes filters and the eval toggle through to the client", async () => {
    render(<LiveTraces isOwner />);
    await waitFor(() => expect(fetchTraces).toHaveBeenCalled());
    await userEvent.selectOptions(screen.getByLabelText("tool"), "search_graph");
    await waitFor(() => expect(fetchTraces).toHaveBeenLastCalledWith(
      expect.objectContaining({ tool: "search_graph" })));
    await userEvent.click(screen.getByLabelText(/include eval traffic/i));
    await waitFor(() => expect(fetchTraces).toHaveBeenLastCalledWith(
      expect.objectContaining({ includeEvals: true })));
    expect(fetchTraceSummary).toHaveBeenLastCalledWith(7, true);
  });

  it("renders token tiles with measurement coverage", async () => {
    render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByText("41,000")).toBeInTheDocument());   // window total
    expect(screen.getByText("1,500")).toBeInTheDocument();       // median per call
    expect(screen.getByText("380")).toBeInTheDocument();         // tokens / result
    // Coverage is load-bearing: 9 of 12 calls measured, so the total is partial.
    expect(screen.getByText(/over 9 of 12 calls/)).toBeInTheDocument();
  });

  it("breaks tokens down by tool, collapsed by default", async () => {
    render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByText("tokens by tool")).toBeInTheDocument());
    const summaryEl = screen.getByText("tokens by tool");
    const details = summaryEl.closest("details")!;
    expect(details).toHaveProperty("open", false);
    await userEvent.click(summaryEl);
    expect(details).toHaveProperty("open", true);
    // Scoped with `within`: "search_graph" also appears as the tool filter's
    // <option> and as the trace row's own tool cell, so an unscoped getByText
    // would match three elements and throw.
    expect(within(details).getByText("search_graph")).toBeInTheDocument();
    expect(within(details).getByText("33,000")).toBeInTheDocument();
    expect(within(details).getByText("8,000")).toBeInTheDocument();
  });

  it("marks error rows", async () => {
    fetchTraces.mockResolvedValue({
      traces: [{ ...trace, error: "boom", confidence_level: null, judgment: null }],
    });
    render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByTitle("boom")).toBeInTheDocument());  // error dot carries the message
  });

  it("renders tiles and filters in one header row", async () => {
    const { container } = render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByText("calls (7d)")).toBeInTheDocument());
    const header = container.querySelector(".live-traces-header");
    expect(header).not.toBeNull();
    expect(within(header as HTMLElement).getByText("calls (7d)")).toBeInTheDocument();
    expect(within(header as HTMLElement).getByLabelText("tool")).toBeInTheDocument();
  });

  it("wraps every tile label in a definition tooltip", async () => {
    const { container } = render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByText("calls (7d)")).toBeInTheDocument());
    for (const label of ["calls (7d)", "p50 latency", "weak or none", "follow-up rate", "errors"]) {
      expect(screen.getByText(label).closest(".dt")).not.toBeNull();
    }
  });

  it("shows per-call tokens, and — for calls predating the measurement", async () => {
    fetchTraces.mockResolvedValue({
      traces: [trace, { ...trace, id: 2, query: "older call",
                        result_bytes: null, result_tokens: null }],
    });
    render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByText("older call")).toBeInTheDocument());
    const measured = screen.getByText("why was the judge deleted").closest("tr")!;
    expect(within(measured).getByText("1,200")).toBeInTheDocument();
    const unmeasured = screen.getByText("older call").closest("tr")!;
    expect(within(unmeasured).getByText("—")).toBeInTheDocument();
  });

  it("offers a replay on search rows, partial when nothing was captured", async () => {
    fetchTraces.mockResolvedValue({ traces: [
      { ...trace, id: 1, has_replay: true },
      { ...trace, id: 2, has_replay: false, tool: "search_memories", strategy: null },
      { ...trace, id: 3, tool: "get_memory", strategy: null },
      { ...trace, id: 4, error: "boom" },
    ] });
    render(<LiveTraces isOwner />);
    await waitFor(() => expect(screen.getByRole("button", { name: "▶ Replay" })).toBeInTheDocument());
    expect(screen.getAllByRole("button", { name: /▶ Replay/ })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "▶ Replay (partial)" })).toBeInTheDocument();
  });

  it("opens the replay overlay without toggling the drill-down", async () => {
    fetchTraces.mockResolvedValue({ traces: [{ ...trace, has_replay: true }] });
    render(<LiveTraces isOwner />);
    await userEvent.click(await screen.findByRole("button", { name: "▶ Replay" }));
    expect(screen.getByText("replay overlay 1")).toBeInTheDocument();
    expect(fetchTraceDetail).not.toHaveBeenCalled();
  });
});

const detail: TraceDetail = {
  ...trace,
  args: { query: "why was the judge deleted", limit: 20 },
  results: [
    { id: "n-1", kind: "memory", rank: 1, score: 0.03, similarity: 0.64,
      via: "concept", label: "Judge deletion ruling" },
    { id: "n-2", kind: "memory", rank: 2, score: 0.01, similarity: 0.5,
      via: "flat", label: "(deleted)" },
  ],
  trace: { stop_reason: "budget", crawl_mode: "deterministic" },
  judgments: [{ id: 9, verdict: "good", judged_by: "owner",
                notes: "spot on", at: "2026-07-29T11:00:00+00:00" }],
};

const GROUP_KEY = "kb.retrieval.groupByRequest";

const turn: Turn = {
  id: 1, session_id: "s-1", caller: null,
  started_at: "2025-01-01T10:00:00+00:00", ended_at: "2025-01-01T10:00:25+00:00",
  calls: 5, tools: { search_memories: 4, get_memory: 1 },
  first_query: "sourdough starter feeding schedule",
  result_bytes: 70000, result_tokens: 20000, errors: 0, outcome: "opened", judgment: null,
};

const turnDetail: TurnDetail = { ...turn, traces: [trace], judgments: [] };

describe("LiveTraces drill-down", () => {
  it("expands a row into args, labelled results, and judgments", async () => {
    fetchTraceDetail.mockResolvedValue(detail);
    render(<LiveTraces isOwner />);
    await waitFor(() =>
      expect(screen.getByText("why was the judge deleted")).toBeInTheDocument());
    await userEvent.click(screen.getByText("why was the judge deleted"));
    await waitFor(() =>
      expect(screen.getByText("Judge deletion ruling")).toBeInTheDocument());
    expect(screen.getByText("(deleted)")).toBeInTheDocument();
    expect(screen.getByText(/"limit": 20/)).toBeInTheDocument();     // args pretty-print
    expect(screen.getByText(/stop_reason/)).toBeInTheDocument();     // crawl trace
    expect(screen.getByText("spot on")).toBeInTheDocument();         // prior judgment
  });

  it("posts a judgment and refreshes the detail", async () => {
    fetchTraceDetail.mockResolvedValue({ ...detail, judgments: [] });
    postTraceJudgment.mockResolvedValue({
      id: 10, verdict: "partial", judged_by: "owner", notes: null,
      at: "2026-07-29T12:00:00+00:00",
    });
    render(<LiveTraces isOwner />);
    await waitFor(() => expect(fetchTraces).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByText("why was the judge deleted")).toBeInTheDocument());
    await userEvent.click(screen.getByText("why was the judge deleted"));
    await waitFor(() => expect(screen.getByText("partial")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "partial" }));
    await waitFor(() =>
      expect(postTraceJudgment).toHaveBeenCalledWith(1, "partial", undefined));
    expect(fetchTraceDetail).toHaveBeenCalledTimes(2);   // refetched after post
    // the parent table's chip + judged-filter must reflect the new verdict too --
    // a second fetchTraces call is the signal that onJudged fired.
    await waitFor(() => expect(fetchTraces).toHaveBeenCalledTimes(2));
  });

  it("offers the replay from inside the drill-down too", async () => {
    fetchTraceDetail.mockResolvedValue(detail);
    render(<LiveTraces isOwner />);
    await userEvent.click(await screen.findByText("why was the judge deleted"));
    await waitFor(() => expect(screen.getByText("Judge deletion ruling")).toBeInTheDocument());
    const buttons = screen.getAllByRole("button", { name: "▶ Replay (partial)" });
    expect(buttons).toHaveLength(2);                 // table row + drill-down
    await userEvent.click(buttons[1]);
    expect(screen.getByText("replay overlay 1")).toBeInTheDocument();
  });
});

describe("LiveTraces grouped by request", () => {
  beforeEach(() => {
    window.localStorage.removeItem(GROUP_KEY);
  });

  it("defaults to grouped and renders request rows with span, tool mix, outcome and tokens", async () => {
    render(<LiveTraces isOwner />);
    expect(await screen.findByText("sourdough starter feeding schedule")).toBeInTheDocument();
    expect(fetchTurns).toHaveBeenCalledTimes(1);
    expect(fetchTraces).not.toHaveBeenCalled();
    expect(screen.getByText("opened")).toHaveClass("outcome-opened");
    expect(screen.getByText("25s")).toBeInTheDocument();                 // span
    expect(screen.getByText("4 search · 1 get")).toBeInTheDocument();    // tool mix
    expect(screen.getByText("20,000")).toBeInTheDocument();              // tokens
    expect(screen.getByText("5")).toBeInTheDocument();                   // calls
  });

  it("marks a request with errors and renders its verdict chip", async () => {
    fetchTurns.mockResolvedValue({ turns: [{ ...turn, errors: 2, outcome: "error", judgment: "bad" }] });
    render(<LiveTraces isOwner />);
    await screen.findByText("sourdough starter feeding schedule");
    expect(screen.getByText("error")).toHaveClass("outcome-error");
    expect(screen.getByTitle("2 call(s) errored")).toBeInTheDocument();
    expect(screen.getByText("bad")).toHaveClass("verdict-bad");
  });

  it("expands a request into its calls, which still offer replay and per-call verdicts", async () => {
    fetchTraceDetail.mockResolvedValue(detail);
    render(<LiveTraces isOwner />);
    await userEvent.click(await screen.findByText("sourdough starter feeding schedule"));
    expect(await screen.findByText("why was the judge deleted")).toBeInTheDocument();  // a call row
    expect(fetchTurnDetail).toHaveBeenCalledWith(1);
    expect(screen.getByRole("button", { name: "▶ Replay (partial)" })).toBeInTheDocument();
    await userEvent.click(screen.getByText("why was the judge deleted"));
    await waitFor(() => expect(screen.getByText("Judge deletion ruling")).toBeInTheDocument());
    expect(screen.getAllByRole("button", { name: "good" })).toHaveLength(2);  // request + call
  });

  it("posts a request-level judgment and refreshes both the detail and the list", async () => {
    postTurnJudgment.mockResolvedValue({
      id: 5, verdict: "good", judged_by: "owner", notes: null, at: "2026-10-09T20:00:00+00:00",
    });
    render(<LiveTraces isOwner />);
    await userEvent.click(await screen.findByText("sourdough starter feeding schedule"));
    await userEvent.click(await screen.findByRole("button", { name: "good" }));
    await waitFor(() => expect(postTurnJudgment).toHaveBeenCalledWith(1, "good", undefined));
    expect(postTraceJudgment).not.toHaveBeenCalled();
    await waitFor(() => expect(fetchTurnDetail).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(fetchTurns).toHaveBeenCalledTimes(2));
  });

  it("switches to the flat table when the toggle is turned off and remembers it", async () => {
    render(<LiveTraces isOwner />);
    await screen.findByText("sourdough starter feeding schedule");
    await userEvent.click(screen.getByRole("checkbox", { name: /group by request/i }));
    expect(await screen.findByText("why was the judge deleted")).toBeInTheDocument();
    expect(fetchTraces).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem(GROUP_KEY)).toBe("false");
  });

  it("passes filters through to the turns client", async () => {
    render(<LiveTraces isOwner />);
    await screen.findByText("sourdough starter feeding schedule");
    await userEvent.selectOptions(screen.getByLabelText("tool"), "get_memory");
    await waitFor(() => expect(fetchTurns).toHaveBeenLastCalledWith(
      expect.objectContaining({ tool: "get_memory" })));
    await userEvent.selectOptions(screen.getByLabelText("judged"), "unjudged");
    await waitFor(() => expect(fetchTurns).toHaveBeenLastCalledWith(
      expect.objectContaining({ judged: false })));
  });

  it("renders the request tiles with the gap in force and definition tooltips", async () => {
    render(<LiveTraces isOwner />);
    await screen.findByText("sourdough starter feeding schedule");
    expect(screen.getByText("44")).toBeInTheDocument();                      // requests
    expect(screen.getByText("6.5")).toBeInTheDocument();                     // calls / request
    expect(screen.getByText("calls ≤ 90 s apart")).toBeInTheDocument();
    expect(screen.getByText("median 2,600 tokens")).toBeInTheDocument();
    expect(screen.getByText("requests that opened a result")).toBeInTheDocument();
    for (const label of ["requests", "calls / request"]) {
      expect(screen.getByText(label).closest(".dt")).not.toBeNull();
    }
  });

  it("refetches turns on mcp_trace change-feed events", async () => {
    render(<LiveTraces isOwner />);
    await screen.findByText("sourdough starter feeding schedule");
    feedCb?.(new Set(["mcp_trace"]));
    await waitFor(() => expect(fetchTurns).toHaveBeenCalledTimes(2));
  });

  it("refreshes an expanded request on mcp_trace change-feed events", async () => {
    render(<LiveTraces isOwner />);
    await userEvent.click(await screen.findByText("sourdough starter feeding schedule"));
    await screen.findByText("why was the judge deleted");               // expansion loaded
    expect(fetchTurnDetail).toHaveBeenCalledTimes(1);
    feedCb?.(new Set(["mcp_trace"]));
    await waitFor(() => expect(fetchTurnDetail).toHaveBeenCalledTimes(2));
  });

  it("shows a failed request verdict inline and keeps the call rows", async () => {
    postTurnJudgment.mockRejectedValueOnce(new Error("boom"));
    render(<LiveTraces isOwner />);
    await userEvent.click(await screen.findByText("sourdough starter feeding schedule"));
    await screen.findByText("why was the judge deleted");
    await userEvent.click(screen.getByRole("button", { name: "good" }));
    expect(await screen.findByText("judgment failed")).toBeInTheDocument();
    expect(screen.getByText("why was the judge deleted")).toBeInTheDocument();  // rows stay
  });

  it("renders an unparseable span as a dash, not NaN", async () => {
    fetchTurns.mockResolvedValue({ turns: [{ ...turn, ended_at: "not-a-date" }] });
    render(<LiveTraces isOwner />);
    const row = (await screen.findByText("sourdough starter feeding schedule")).closest("tr")!;
    expect(within(row).getAllByRole("cell")[1].textContent).toBe("—");
  });
});

describe("LiveTraces linked request", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("opens the request named by ?turn= above the list and close clears it", async () => {
    window.history.replaceState(null, "", "/?view=retrieval&turn=7");
    fetchTurnDetail.mockResolvedValue({ ...turnDetail, id: 7 });
    render(<LiveTraces isOwner />);
    const linked = await screen.findByRole("region", { name: "Request #7" });
    await waitFor(() => expect(fetchTurnDetail).toHaveBeenCalledWith(7));
    expect(await within(linked).findByText("why was the judge deleted")).toBeInTheDocument();
    await userEvent.click(within(linked).getByRole("button", { name: "close" }));
    expect(screen.queryByRole("region", { name: "Request #7" })).toBeNull();
    expect(new URLSearchParams(window.location.search).get("turn")).toBeNull();
  });

  it("scrolls the linked request into view", async () => {
    const scroll = vi.fn();
    const orig = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    try {
      window.history.replaceState(null, "", "/?view=retrieval&turn=7");
      fetchTurnDetail.mockResolvedValue({ ...turnDetail, id: 7 });
      render(<LiveTraces isOwner />);
      const linked = await screen.findByRole("region", { name: "Request #7" });
      await waitFor(() => expect(scroll).toHaveBeenCalled());
      expect(scroll.mock.contexts[0]).toBe(linked);
      expect(scroll).toHaveBeenCalledWith({ block: "start" });
    } finally {
      Element.prototype.scrollIntoView = orig;
    }
  });

  it("ignores a malformed ?turn=", async () => {
    window.history.replaceState(null, "", "/?view=retrieval&turn=abc");
    render(<LiveTraces isOwner />);
    expect((await screen.findAllByText("why was the judge deleted")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("region", { name: /Request #/ })).toBeNull();
    expect(fetchTurnDetail).not.toHaveBeenCalled();
  });

  it("offers the skill tools in the tool filter", async () => {
    render(<LiveTraces isOwner />);
    const select = await screen.findByLabelText("tool");
    for (const t of ["search_skills", "get_skill", "get_skill_file"]) {
      expect(within(select).getByRole("option", { name: t })).toBeInTheDocument();
    }
  });
});
