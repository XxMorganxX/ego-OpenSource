import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { NodeMeta } from "../types";

const fetchProcessing = vi.fn();
vi.mock("../api/client", () => ({ fetchProcessing: () => fetchProcessing() }));

// Capture the change-feed callback so tests can fire live "nodes changed" events by hand
// (the real socket no-ops under jsdom).
let feedCb: ((tables: Set<string>) => void) | undefined;
vi.mock("../hooks/useChangeFeed", () => ({
  useChangeFeed: (cb: (tables: Set<string>) => void) => {
    feedCb = cb;
  },
}));

import { AnalysisToasts } from "./AnalysisToasts";

function meta(over: Partial<NodeMeta> & { id: string }): NodeMeta {
  return {
    path: `notes.${over.id}`, parent_id: null, type: "note", slug: over.id,
    title: null, tags: [], importance: 1, ...over,
  };
}

describe("AnalysisToasts", () => {
  beforeEach(() => {
    feedCb = undefined;
    fetchProcessing.mockReset();
  });

  it("toasts a memory that newly enters analysis", async () => {
    fetchProcessing
      .mockResolvedValueOnce({ nodes: [] }) // baseline poll
      .mockResolvedValueOnce({ nodes: [meta({ id: "n1", title: "Atlas" })] });
    render(<AnalysisToasts />);
    await waitFor(() => expect(fetchProcessing).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/Analyzing/)).toBeNull();

    await act(async () => {
      feedCb!(new Set(["nodes"]));
    });
    expect(await screen.findByText("Atlas")).toBeInTheDocument();
    expect(screen.getByText(/Analyzing/)).toBeInTheDocument();
  });

  it("does not announce memories already mid-analysis at load (baseline only)", async () => {
    fetchProcessing.mockResolvedValue({ nodes: [meta({ id: "x", title: "Existing" })] });
    render(<AnalysisToasts />);
    await waitFor(() => expect(fetchProcessing).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("Existing")).toBeNull();
  });

  it("falls back to the slug when a memory has no title", async () => {
    fetchProcessing
      .mockResolvedValueOnce({ nodes: [] })
      .mockResolvedValueOnce({ nodes: [meta({ id: "n2", title: null, slug: "my-note" })] });
    render(<AnalysisToasts />);
    await waitFor(() => expect(fetchProcessing).toHaveBeenCalledTimes(1));
    await act(async () => {
      feedCb!(new Set(["nodes"]));
    });
    expect(await screen.findByText("my-note")).toBeInTheDocument();
  });

  it("ignores changes to tables other than nodes", async () => {
    fetchProcessing.mockResolvedValue({ nodes: [] });
    render(<AnalysisToasts />);
    await waitFor(() => expect(fetchProcessing).toHaveBeenCalledTimes(1));
    await act(async () => {
      feedCb!(new Set(["ingest_log"]));
    });
    expect(fetchProcessing).toHaveBeenCalledTimes(1); // no extra poll
  });
});
