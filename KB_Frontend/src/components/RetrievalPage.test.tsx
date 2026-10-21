import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useRef } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EvalsPayload } from "../types";
import { DEFS } from "./defs";

const fetchRetrievalEvals = vi.fn();
// RetrievalPage now renders McpToolsPanel (section 5), which pulls these from the same
// module — mocked here too so the panel's own fetch/auth calls don't blow up in a
// RetrievalPage-focused render. McpToolsPanel.test.tsx exercises the panel's own behavior.
const fetchMcpTools = vi.fn();
const setMcpToolEnabled = vi.fn();
const dbAuth = vi.fn();
const hasDbPassword = vi.fn();
const storeDbPassword = vi.fn();
vi.mock("../api/client", () => ({
  fetchRetrievalEvals: () => fetchRetrievalEvals(),
  fetchMcpTools: () => fetchMcpTools(),
  setMcpToolEnabled: (t: string, e: boolean) => setMcpToolEnabled(t, e),
  dbAuth: (p: string) => dbAuth(p),
  hasDbPassword: () => hasDbPassword(),
  storeDbPassword: (p: string) => storeDbPassword(p),
}));

// Section 4 is self-contained and has its own suite (LiveTraces.test.tsx); stubbing it
// keeps this file about the page's composition rather than the trace table's behavior.
vi.mock("./LiveTraces", () => ({
  LiveTraces: () => <div>live-traces-stub</div>,
}));

// Section 5 (McpToolsPanel) subscribes to the change feed independently of anything the
// page itself does, so a single last-write-wins ref is not enough to fake the feed —
// this tracks every live subscriber (mirroring the real Set-based subscribe /
// unsubscribe-on-unmount contract of useChangeFeed) and broadcastChange notifies all of
// them, exactly like one shared socket would. Do not collapse this back to one callback:
// the moment a second section subscribes, the first one's events vanish silently.
let feedCbs: Array<(tables: Set<string>) => void> = [];
function broadcastChange(tables: Set<string>): void {
  feedCbs.forEach((fn) => fn(tables));
}
vi.mock("../hooks/useChangeFeed", () => ({
  useChangeFeed: (cb: (tables: Set<string>) => void) => {
    const ref = useRef(cb);
    ref.current = cb;
    useEffect(() => {
      const fn = (tables: Set<string>) => ref.current(tables);
      feedCbs.push(fn);
      return () => {
        feedCbs = feedCbs.filter((f) => f !== fn);
      };
    }, []);
  },
}));

import { RetrievalPage } from "./RetrievalPage";

const evals: EvalsPayload = {
  reference_runs: [
    { ran_at: "20250301T120000Z", overall_must_recall: 0.6, mrr: 0.4,
      items_scored: 30, items_total: 40, by_archetype: {} },
    { ran_at: "20250315T120000Z", overall_must_recall: 0.8, mrr: 0.5,
      items_scored: 36, items_total: 40, by_archetype: {} },
  ],
  grids: [{
    date: "2025-03-15", git_heads: ["0123456"],
    cells: [{ cell: "cell1", set: "flat", strategy: "hybrid", judge: null,
              repeats: 2, must_recall: 0.875, neg_hits: 3,
              latency_p50_ms: 100, latency_p90_ms: 150,
              by_archetype: { meetings: 0.9 } }],
  }],
  off_corpus: { verdict: "FAIL", threshold: 0.5, worst_best_similarity: 0.61,
                probes: [] },
  ablations: { sweeps: [] },
  warnings: ["stray.json: unrecognized shape"],
};

beforeEach(() => {
  feedCbs = [];
  fetchRetrievalEvals.mockReset().mockResolvedValue(evals);
  // McpToolsPanel (section 5) fetches independently; empty tools short-circuits it to
  // its own "not published" state, which this file doesn't otherwise assert on.
  fetchMcpTools.mockReset().mockResolvedValue({ tools: [] });
  hasDbPassword.mockReset().mockReturnValue(true);
});

describe("RetrievalPage", () => {
  it("renders scorecard, grid, and the live-traces section from fixtures", async () => {
    render(<RetrievalPage isOwner />);
    // scorecard: latest flat recall + off-corpus verdict
    // (the fixture's single grid cell also renders "0.875" in the grid table below,
    // so scope the query to the scorecard tile to disambiguate).
    const flatTile = (await screen.findByText("flat recall@20")).closest(".retrieval-tile");
    expect(within(flatTile as HTMLElement).getByText("0.875")).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    // benchmark grid row
    expect(screen.getByText("cell1")).toBeInTheDocument();
    // parse warnings surface
    expect(screen.getByText(/stray\.json/)).toBeInTheDocument();
    // live traces section renders the self-contained child
    expect(screen.getByText("Live traces")).toBeInTheDocument();
    expect(screen.getByText("live-traces-stub")).toBeInTheDocument();
    // and section 5 is mounted alongside it
    expect(screen.getByText("MCP tools")).toBeInTheDocument();
  });

  // The page's own live-search subscription moved into LiveTraces (stubbed above), so
  // section 5 is now the subscriber this composition has to keep fed. Broadcasting
  // through the multi-subscriber fake is what proves the wiring survives; with a
  // single-callback fake this passes for the wrong reason and regresses the moment a
  // second section subscribes.
  it("routes change-feed events to section 5's own subscriber", async () => {
    render(<RetrievalPage isOwner />);
    await screen.findByText("cell1");
    expect(fetchMcpTools).toHaveBeenCalledTimes(1);
    await act(async () => {
      broadcastChange(new Set(["mcp_tool_state"]));
    });
    await waitFor(() => expect(fetchMcpTools).toHaveBeenCalledTimes(2));
    expect(fetchRetrievalEvals).toHaveBeenCalledTimes(1); // files did not change
  });

  it("shows a tenant only Live traces: no evals fetch, no eval band, no MCP tools", async () => {
    const { unmount } = render(<RetrievalPage isOwner />);
    expect(await screen.findByText("cell1")).toBeInTheDocument();
    expect(screen.getByText("MCP tools")).toBeInTheDocument();
    unmount();
    fetchRetrievalEvals.mockClear();
    fetchMcpTools.mockClear();

    render(<RetrievalPage isOwner={false} />);
    expect(screen.getByText("live-traces-stub")).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
    expect(screen.queryByText("flat recall@20")).not.toBeInTheDocument();
    expect(screen.queryByText("MCP tools")).not.toBeInTheDocument();
    expect(fetchRetrievalEvals).not.toHaveBeenCalled(); // owner-only route: never asked
    expect(fetchMcpTools).not.toHaveBeenCalled();
  });

  it("renders empty states when there is no data", async () => {
    fetchRetrievalEvals.mockResolvedValue({
      reference_runs: [], grids: [], off_corpus: null, ablations: null, warnings: [],
    });
    render(<RetrievalPage isOwner />);
    expect(await screen.findByText("No benchmark grids yet.")).toBeInTheDocument();
  });

  it("drops fetch resolutions that land after unmount", async () => {
    let resolveEvals!: (v: EvalsPayload) => void;
    fetchRetrievalEvals.mockReturnValue(
      new Promise<EvalsPayload>((r) => {
        resolveEvals = r;
      }),
    );
    const { unmount } = render(<RetrievalPage isOwner />);
    unmount();
    await act(async () => {
      resolveEvals(evals);
    });
    // Reaching here without React warnings/errors is the contract; the liveRef
    // guard is what keeps it true regardless of React version behavior.
  });

  it("renders the glossary collapsed and opens on click", async () => {
    render(<RetrievalPage isOwner />);
    await screen.findByText("cell1");
    const summary = screen.getByText("What these numbers mean");
    const details = summary.closest("details")!;
    expect(details).toHaveProperty("open", false);
    await userEvent.click(summary);
    expect(details).toHaveProperty("open", true);
    expect(screen.getByText(DEFS.neg_hits.long)).toBeInTheDocument();
  });

  it("defines the scorecard tile labels", async () => {
    render(<RetrievalPage isOwner />);
    await screen.findByText("cell1");
    const label = screen.getByText("flat recall@20");
    const tip = label.closest(".dt")?.querySelector('[role="tooltip"]');
    expect(tip?.textContent).toBe(DEFS.must_recall.short);
  });

  it("packs tiles, trend, and grid into one eval band", async () => {
    const { container } = render(<RetrievalPage isOwner />);
    await waitFor(() =>
      expect(screen.getByText("flat recall@20")).toBeInTheDocument());
    const band = container.querySelector(".retrieval-eval-band");
    expect(band).not.toBeNull();
    // all three exhibits live inside the band, not in separate sections
    expect(within(band as HTMLElement).getByText("flat recall@20")).toBeInTheDocument();
    expect(within(band as HTMLElement).getByText("Reference-run trend")).toBeInTheDocument();
    expect(within(band as HTMLElement).getByText("Benchmark grid")).toBeInTheDocument();
  });

  it("defines the flat and package sets in the glossary", async () => {
    render(<RetrievalPage isOwner />);
    await waitFor(() =>
      expect(screen.getByText("flat set (SET-FLAT)")).toBeInTheDocument());
    expect(screen.getByText("package set (SET-PACKAGE)")).toBeInTheDocument();
    // rot repairs took: the stale claims are gone from the glossary's long copy
    expect(screen.queryByText(/timing starts after the query embedding/)).toBeNull();
    expect(screen.queryByText(/skewed until those rows age out/)).toBeNull();
  });

  it("captions the eval tiles with set framing and the decoded junk line", async () => {
    render(<RetrievalPage isOwner />);
    await waitFor(() =>
      expect(screen.getByText("find-the-note set")).toBeInTheDocument());
    expect(
      screen.getByText("set-membership — the hard set (expect lower)"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/worst junk probe 0.61 vs the 0.5 "weak" bar/),
    ).toBeInTheDocument();
  });

  it("defines the live-trace metrics in the glossary", async () => {
    render(<RetrievalPage isOwner />);
    await waitFor(() =>
      expect(screen.getByText("flat set (SET-FLAT)")).toBeInTheDocument());
    for (const label of ["calls", "weak or none", "follow-up rate", "errors"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    // spot-check one long text landed
    expect(screen.getByText(/implicit did-it-help signal/)).toBeInTheDocument();
  });
});
