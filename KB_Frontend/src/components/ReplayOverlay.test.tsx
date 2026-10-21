import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CRAWL, LABELS, PARTIAL } from "../test/replayFixtures";
import type { TraceReplay } from "../types";

const fetchTraceReplay = vi.fn();
vi.mock("../api/client", () => ({
  fetchTraceReplay: (...a: unknown[]) => fetchTraceReplay(...a),
}));
vi.mock("./DetailPanel", () => ({
  DetailPanel: ({ nodeId }: { nodeId: string }) => <div>detail for {nodeId}</div>,
}));

import { ReplayOverlay } from "./ReplayOverlay";

const payload = (over: Partial<TraceReplay> = {}): TraceReplay => ({
  trace_id: 7, tool: "search_graph", query: "postgres tuning", at: "2026-10-09T10:00:00+00:00",
  args: { query: "postgres tuning" }, completeness: "full", replay: CRAWL, labels: LABELS, ...over,
});

beforeEach(() => vi.clearAllMocks());

describe("ReplayOverlay", () => {
  it("loads the replay and steps through stages", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/stage 1\/8: query/)).toBeInTheDocument());
    expect(fetchTraceReplay).toHaveBeenCalledWith(7);
    await userEvent.click(screen.getByRole("button", { name: "next stage" }));
    expect(screen.getByText("stage 2/8: keyword search — 2 keyword hits")).toBeInTheDocument();
  });

  it("marks a partial replay and disables unrecorded stages", async () => {
    fetchTraceReplay.mockResolvedValue(payload({ tool: "search_memories", completeness: "partial", replay: PARTIAL }));
    render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("partial")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "keyword search" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "cut" })).toBeEnabled();
  });

  it("names the parent and hop that offered a hovered crawl row", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    const { container } = render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('[data-node="x"]')).not.toBeNull());
    fireEvent.mouseEnter(container.querySelector('[data-node="x"]')!);
    expect(screen.getByRole("tooltip")).toHaveTextContent("via Postgres (hop 1)");
    expect(screen.getByRole("tooltip")).toHaveTextContent("admitted");
    expect(screen.getByRole("tooltip")).toHaveTextContent("sim#2");
  });

  it("names a hovered hub -- its graph label may have been dropped for space", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    const { container } = render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('[data-node="hub:k1"]')).not.toBeNull());
    fireEvent.mouseEnter(container.querySelector('[data-node="hub:k1"]')!);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Postgres");
    expect(screen.getByRole("tooltip")).toHaveTextContent("starting point · label-cosine");
  });

  it("shows the four-list fusion panel on the final-merge stage only", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/stage 1\/8/)).toBeInTheDocument());
    expect(screen.queryByRole("region", { name: "final merge lists" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "final merge" }));
    expect(screen.getByRole("region", { name: "final merge lists" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "cut" }));
    expect(screen.queryByRole("region", { name: "final merge lists" })).toBeNull();
  });

  it("pins a clicked memory and shows its path, with the memory itself one click away", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    const { container } = render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('[data-node="a"]')).not.toBeNull());
    fireEvent.click(container.querySelector('[data-node="a"]')!);
    const path = screen.getByRole("region", { name: "memory path" });
    expect(path).toHaveTextContent("Alpha");
    expect(path).toHaveTextContent("#1 of 2");
    expect(path).toHaveTextContent("cut at #3, 1 place past the limit (2)");
    expect(container.querySelector('[data-node="a"]')!.getAttribute("class")).toContain("pinned");
    await userEvent.click(screen.getByRole("button", { name: "open memory" }));
    expect(screen.getByText("detail for a")).toBeInTheDocument();
  });

  it("highlights the path step for the stage on screen", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    const { container } = render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('[data-node="x"]')).not.toBeNull());
    fireEvent.click(container.querySelector('[data-node="x"]')!);
    await userEvent.click(screen.getByRole("button", { name: "crawl hop 1" }));
    const current = screen.getByRole("region", { name: "memory path" }).querySelector("li.current")!;
    expect(current).toHaveTextContent("offered in hop 1 via Postgres — admitted");
  });

  it("unpins from the path card", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    const { container } = render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('[data-node="a"]')).not.toBeNull());
    fireEvent.click(container.querySelector('[data-node="a"]')!);
    await userEvent.click(screen.getByRole("button", { name: "unpin" }));
    expect(screen.queryByRole("region", { name: "memory path" })).toBeNull();
  });

  it("finds a candidate by title and pins it", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/stage 1\/8/)).toBeInTheDocument());
    await userEvent.type(screen.getByRole("searchbox", { name: "find a memory" }), "gam");
    await userEvent.click(screen.getByRole("button", { name: "Gamma" }));
    expect(screen.getByRole("region", { name: "memory path" })).toHaveTextContent("Gamma");
  });

  it("says when no candidate matches -- every search missed it", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/stage 1\/8/)).toBeInTheDocument());
    await userEvent.type(screen.getByRole("searchbox", { name: "find a memory" }), "zebra");
    expect(screen.getByText("No candidate matches “zebra” — keyword, meaning and crawl search all missed it."))
      .toBeInTheDocument();
  });

  it("does not claim a miss on a partial replay", async () => {
    fetchTraceReplay.mockResolvedValue(payload({ tool: "search_memories", completeness: "partial", replay: PARTIAL }));
    render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("partial")).toBeInTheDocument());
    await userEvent.type(screen.getByRole("searchbox", { name: "find a memory" }), "zebra");
    expect(screen.getByText(/No returned result matches “zebra” — this trace predates full capture/))
      .toBeInTheDocument();
  });

  it("summarises where the returned page came from on the cut stage", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/stage 1\/8/)).toBeInTheDocument());
    expect(screen.queryByRole("note", { name: "outcome" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "cut" }));
    expect(screen.getByRole("note", { name: "outcome" }))
      .toHaveTextContent("1 both · 1 from crawl · final merge swapped 1 of 2 · crawl admitted 1, 1 reached the page");
  });

  it("shows an error when the replay cannot load", async () => {
    fetchTraceReplay.mockRejectedValue(new Error("404"));
    render(<ReplayOverlay isOwner traceId={7} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("failed to load replay")).toBeInTheDocument());
  });

  it("closes on Escape and on the close button", async () => {
    fetchTraceReplay.mockResolvedValue(payload());
    const onClose = vi.fn();
    render(<ReplayOverlay isOwner traceId={7} onClose={onClose} />);
    await waitFor(() => expect(screen.getByText(/stage 1\/8/)).toBeInTheDocument());
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
