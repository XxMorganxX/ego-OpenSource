import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { IngestEvent, IngestStatus } from "../types";

const fetchIngestLog = vi.fn();
const fetchIngestStatus = vi.fn();
const retryFailedIngest = vi.fn();
const dismissStuckRecord = vi.fn();
vi.mock("../api/client", () => ({
  fetchIngestLog: (limit: number, source?: string | null, event?: string | null) =>
    fetchIngestLog(limit, source, event),
  fetchIngestStatus: () => fetchIngestStatus(),
  retryFailedIngest: (opts: unknown) => retryFailedIngest(opts),
  dismissStuckRecord: (source: string, externalId: string) =>
    dismissStuckRecord(source, externalId),
}));

import { IngestionPage } from "./IngestionPage";

const status: IngestStatus = {
  enrichment: { processing: 0, pending: 0, done: 40, failed: 2 },
  pauses: [],
  stuck: [],
  sources: [{ source: "obsidian", last_synced_at: new Date().toISOString(), cursor: null }],
};

function ev(over: Partial<IngestEvent> & { id: number }): IngestEvent {
  return {
    at: new Date().toISOString(), event: "node_created", source: "obsidian",
    external_id: null, node_id: `n${over.id}`, path: "notes.alpha", content_hash: null,
    detail: {}, ...over,
  };
}
function failure(id: number, error: string, node_id: string, path: string): IngestEvent {
  return ev({ id, event: "enrichment_failed", node_id, path, detail: { error } });
}

// One node (Atlas) appears twice — created then failed — to exercise node grouping.
const baseLog = {
  events: [
    failure(3, "InferenceError: inference call failed: timed out", "n-timeout", "Vandelay.Corp.Atlas"),
    failure(2, "UniqueViolation: duplicate key value violates ...", "n-dup", "TeamHub.Onboarding_Checklist"),
    ev({ id: 6, event: "node_created", node_id: "n-timeout", path: "Vandelay.Corp.Atlas",
      at: new Date(Date.now() - 3_600_000).toISOString() }),
    ev({ id: 5, event: "enrichment_done", node_id: "nb", path: "notes.beta" }),
    ev({ id: 4, event: "node_created", node_id: "na", path: "notes.alpha" }),
  ],
};

describe("IngestionPage", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/"); // reset URL-backed filters between tests
    retryFailedIngest.mockReset().mockResolvedValue({ requeued: 1 });
    dismissStuckRecord.mockReset().mockResolvedValue({ ok: true });
    fetchIngestStatus.mockReset().mockResolvedValue(status);
    fetchIngestLog.mockReset().mockResolvedValue(baseLog);
  });

  it("renders the rollup and humanized item titles", async () => {
    render(<IngestionPage />);
    await waitFor(() => expect(screen.getByText("5 in view")).toBeInTheDocument());
    // path leaf, underscores → spaces, machine path kept dim alongside
    expect(screen.getAllByText("Atlas").length).toBeGreaterThan(0);
    expect(screen.getByText("TeamHub.Onboarding_Checklist")).toBeInTheDocument();
    // event-type rollup chips
    expect(screen.getByText(/1 enriched/)).toBeInTheDocument();
    expect(screen.getByText(/2 failed/)).toBeInTheDocument();
  });

  it("classifies failures as retryable vs needs-fix", async () => {
    render(<IngestionPage />);
    expect(await screen.findByText(/Inference timeout.*retryable/)).toBeInTheDocument();
    expect(screen.getByText(/Duplicate slug.*needs fix/)).toBeInTheDocument();
    // the raw reason is still shown on the failing row
    expect(screen.getByText(/inference call failed: timed out/)).toBeInTheDocument();
  });

  it("retries only the retryable failures by node id", async () => {
    render(<IngestionPage />);
    const retry = await screen.findByRole("button", { name: /Retry 1 retryable/i });
    await userEvent.click(retry);
    await waitFor(() =>
      expect(retryFailedIngest).toHaveBeenCalledWith({ nodeIds: ["n-timeout"] }),
    );
    expect(await screen.findByText(/Re-queued 1 for another pass/)).toBeInTheDocument();
  });

  it("clearly marks a memory as re-queued after retrying it", async () => {
    render(<IngestionPage />);
    await userEvent.click(await screen.findByRole("button", { name: /Retry 1 retryable/i }));
    // the failing row now reads "re-queued" rather than its error class
    expect(await screen.findByText("↻ Re-queued")).toBeInTheDocument();
    // the rollup reflects it, and the retryable button is gone (nothing left to retry)
    expect(screen.getByText(/1 re-queued/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /retryable/i })).toBeNull(),
    );
  });

  it("offers an escape hatch that re-queues everything by source", async () => {
    render(<IngestionPage />);
    const all = await screen.findByRole("button", { name: /Retry all anyway/i });
    await userEvent.click(all);
    await waitFor(() => expect(retryFailedIngest).toHaveBeenCalledWith({ source: null }));
  });

  it("scopes the escape-hatch retry to the active source filter", async () => {
    render(<IngestionPage />);
    await userEvent.click(await screen.findByRole("button", { name: /obsidian/i }));
    await waitFor(() =>
      expect(fetchIngestLog).toHaveBeenLastCalledWith(expect.any(Number), "obsidian", null),
    );
    await userEvent.click(await screen.findByRole("button", { name: /Retry all anyway/i }));
    await waitFor(() => expect(retryFailedIngest).toHaveBeenCalledWith({ source: "obsidian" }));
  });

  it("funnels the feed to failures", async () => {
    render(<IngestionPage />);
    await userEvent.click(await screen.findByRole("button", { name: /Failures/i }));
    await waitFor(() =>
      expect(fetchIngestLog).toHaveBeenLastCalledWith(expect.any(Number), null, "enrichment_failed"),
    );
  });

  it("collapses to one row per node in the by-node view", async () => {
    render(<IngestionPage />);
    // log view: Atlas shows twice (created + failed)
    await waitFor(() => expect(screen.getAllByText("Atlas")).toHaveLength(2));
    await userEvent.click(screen.getByRole("button", { name: /By node/i }));
    // node view: Atlas collapses to a single lifecycle row
    await waitFor(() => expect(screen.getAllByText("Atlas")).toHaveLength(1));
  });

  it("expands a row to reveal the opaque ids", async () => {
    render(<IngestionPage />);
    await waitFor(() => expect(screen.getByText("beta")).toBeInTheDocument());
    expect(screen.queryByText("nb")).toBeNull();
    await userEvent.click(screen.getByText("beta"));
    expect(await screen.findByText("nb")).toBeInTheDocument();
  });

  it("keeps every source listed when the table is filtered to one", async () => {
    const all = [
      ev({ id: 1, source: "obsidian", node_id: "o1", path: "notes.alpha" }),
      ev({ id: 2, source: "entity", node_id: "e1", path: "entities.people.zelda" }),
      ev({ id: 3, source: "tree", node_id: "t1", path: "Vandelay.Evals" }),
    ];
    // Mimic the real backend: a source filter narrows the returned events.
    fetchIngestLog.mockImplementation((_limit: number, source?: string | null) =>
      Promise.resolve({ events: source ? all.filter((e) => e.source === source) : all }),
    );
    render(<IngestionPage />);
    expect(await screen.findByRole("button", { name: /entity/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /tree/i })).toBeInTheDocument();

    // Filter to obsidian — the other source rows must NOT vanish.
    await userEvent.click(screen.getByRole("button", { name: /obsidian/i }));
    await waitFor(() =>
      expect(fetchIngestLog).toHaveBeenLastCalledWith(expect.any(Number), "obsidian", null),
    );
    expect(screen.getByRole("button", { name: /entity/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /tree/i })).toBeInTheDocument();
  });

  it("lights the LED while enrichment is processing", async () => {
    fetchIngestStatus.mockResolvedValue({
      ...status,
      enrichment: { processing: 1, pending: 3, done: 40 },
    });
    render(<IngestionPage />);
    await waitFor(() => expect(screen.getByText("Ingestion running")).toBeInTheDocument());
    expect(screen.getByText(/Enriching 1 node now/)).toBeInTheDocument();
  });

  it("treats a queued backlog as running, not idle", async () => {
    fetchIngestStatus.mockResolvedValue({
      ...status,
      enrichment: { processing: 0, pending: 5, done: 40 },
    });
    render(<IngestionPage />);
    await waitFor(() => expect(screen.getByText("Ingestion running")).toBeInTheDocument());
    expect(screen.getByText(/5 queued for enrichment/)).toBeInTheDocument();
  });

  it("reports idle when nothing is processing or queued", async () => {
    fetchIngestStatus.mockResolvedValue({ ...status, enrichment: { done: 40 } });
    render(<IngestionPage />);
    await waitFor(() => expect(screen.getByText("Idle")).toBeInTheDocument());
    expect(screen.getByText("No enrichment in flight")).toBeInTheDocument();
  });

  it("shows which model backends enriched a node in the by-node view", async () => {
    fetchIngestLog.mockResolvedValue({
      events: [
        ev({ id: 5, event: "enrichment_done", node_id: "nb", path: "notes.beta",
          detail: { providers: ["claude-cli:claude-sonnet-5", "local:gemma4:26b"] } }),
      ],
    });
    render(<IngestionPage />);
    await userEvent.click(await screen.findByRole("button", { name: /By node/i }));
    const chip = await screen.findByText("claude-cli · local");
    expect(chip).toHaveAttribute("title", "claude-cli:claude-sonnet-5, local:gemma4:26b");
    // expanding the row reveals the full provider:model strings
    await userEvent.click(screen.getByText("beta"));
    expect(
      await screen.findByText("claude-cli:claude-sonnet-5, local:gemma4:26b"),
    ).toBeInTheDocument();
  });

  it("shows no provider chip for rows that predate capture", async () => {
    render(<IngestionPage />);
    await userEvent.click(await screen.findByRole("button", { name: /By node/i }));
    await waitFor(() => expect(screen.getAllByText("Atlas")).toHaveLength(1));
    expect(document.querySelector(".prov-chip")).toBeNull();
  });

  it("shows the reason text for an ingest_failed latest event in the by-node view", async () => {
    // ingest_failed rows carry the failure reason like enrichment_failed does, but g.failure
    // (from groupByNode) is only set for FAILED_EVENT (enrichment_failed) -- the row must
    // still surface the reason for a node whose latest event is ingest_failed.
    fetchIngestLog.mockResolvedValue({
      events: [
        ev({
          id: 7, event: "ingest_failed", node_id: "n-stuck-node", path: "notes.stuck",
          detail: { error: "PathConflict: a node already exists at notes.stuck" },
        }),
      ],
    });
    render(<IngestionPage />);
    await userEvent.click(await screen.findByRole("button", { name: /By node/i }));
    expect(await screen.findByText(/PathConflict: a node already exists/)).toBeInTheDocument();
  });

  it("banners an active provider pause", async () => {
    fetchIngestStatus.mockResolvedValue({
      ...status,
      pauses: [{ provider: "claude-cli", until: "2025-03-01T09:30:00Z", reason: "session limit" }],
    });
    render(<IngestionPage />);
    expect(await screen.findByText(/claude-cli paused/)).toBeInTheDocument();
  });

  it("banners the tenant agent's last contact from agent_last_seen_at", async () => {
    fetchIngestStatus.mockResolvedValue({
      ...status,
      agent_last_seen_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
    });
    render(<IngestionPage />);
    expect(await screen.findByText("agent seen 2 h ago")).toBeInTheDocument();
  });

  it("shows no agent banner when the agent never connected or the backend predates it", async () => {
    // A pause rides along so its banner proves the status has rendered before asserting.
    const paused = {
      ...status,
      pauses: [{ provider: "claude-cli", until: "2025-03-01T09:30:00Z", reason: "session limit" }],
    };
    fetchIngestStatus.mockResolvedValue({ ...paused, agent_last_seen_at: null });
    const { unmount } = render(<IngestionPage />);
    await screen.findByText(/claude-cli paused/);
    expect(screen.queryByText(/agent seen/)).not.toBeInTheDocument();
    unmount();
    fetchIngestStatus.mockResolvedValue(paused); // field absent: pre-tenancy backend
    render(<IngestionPage />);
    await screen.findByText(/claude-cli paused/);
    expect(screen.queryByText(/agent seen/)).not.toBeInTheDocument();
  });

  const stuckNote = {
    source: "obsidian",
    external_id: "Projects/Recipe-Box/plan.md",
    node_id: "n-stuck",
    path: "Obsidian.Projects.Recipe_Box.plan",
    error: "ValueError: PostgreSQL text fields cannot contain NUL (0x00) bytes",
    since: new Date(Date.now() - 3_600_000).toISOString(),
  };

  it("banners a stuck record with its item, error and age", async () => {
    fetchIngestStatus.mockResolvedValue({ ...status, stuck: [stuckNote] });
    render(<IngestionPage />);
    const banner = await screen.findByTestId("stuck-banner");
    expect(banner).toHaveTextContent("obsidian");
    expect(banner).toHaveTextContent("plan"); // titleOf(path)
    expect(banner).toHaveTextContent(/cannot contain NUL/);
    expect(banner).toHaveTextContent(/since .*ago/);
  });

  it("falls back to the external id when a stuck record has no path", async () => {
    fetchIngestStatus.mockResolvedValue({
      ...status,
      stuck: [{ ...stuckNote, source: "webhook", external_id: "inbox#42", path: null }],
    });
    render(<IngestionPage />);
    expect(await screen.findByText("inbox#42")).toBeInTheDocument();
  });

  it("shows no stuck banner when nothing is stuck", async () => {
    render(<IngestionPage />);
    await waitFor(() => expect(screen.getByText("5 in view")).toBeInTheDocument());
    expect(screen.queryByTestId("stuck-banner")).toBeNull();
  });

  it("dismisses a stuck record through the client and reloads", async () => {
    // Stuck on every load until the server-side write happens; then the reload clears it.
    // (Mimics the real backend rather than counting loads, which the page doesn't promise.)
    fetchIngestStatus.mockResolvedValue({ ...status, stuck: [stuckNote] });
    dismissStuckRecord.mockImplementation(async () => {
      fetchIngestStatus.mockResolvedValue(status);
      return { ok: true };
    });
    render(<IngestionPage />);
    await userEvent.click(await screen.findByRole("button", { name: /Dismiss/i }));
    await waitFor(() =>
      expect(dismissStuckRecord).toHaveBeenCalledWith("obsidian", "Projects/Recipe-Box/plan.md"),
    );
    // not optimistic: the banner leaves only once the reload says so
    await waitFor(() => expect(screen.queryByTestId("stuck-banner")).toBeNull());
  });

  it("reports a failed dismiss instead of hiding the record", async () => {
    fetchIngestStatus.mockResolvedValue({ ...status, stuck: [stuckNote] });
    dismissStuckRecord.mockRejectedValue(new Error("500"));
    render(<IngestionPage />);
    await userEvent.click(await screen.findByRole("button", { name: /Dismiss/i }));
    expect(await screen.findByText(/Dismiss failed/)).toBeInTheDocument();
    expect(screen.getByTestId("stuck-banner")).toBeInTheDocument();
  });

  it("tracks the dismiss busy state per row, not globally", async () => {
    const secondStuck = {
      source: "webhook",
      external_id: "inbox#7",
      node_id: "n-stuck-2",
      path: null,
      error: "boom",
      since: new Date(Date.now() - 1_800_000).toISOString(),
    };
    fetchIngestStatus.mockResolvedValue({ ...status, stuck: [stuckNote, secondStuck] });
    // Never resolves during this test — pins the busy state on just the clicked row. Safe to
    // leave pending: this is the suite's last interaction, and beforeEach re-mocks with
    // mockReset() before the next test file run.
    dismissStuckRecord.mockReturnValueOnce(new Promise(() => {}));
    render(<IngestionPage />);
    const buttons = await screen.findAllByRole("button", { name: /Dismiss/i });
    expect(buttons).toHaveLength(2);
    await userEvent.click(buttons[0]);
    await waitFor(() => expect(buttons[0]).toHaveTextContent("Dismissing…"));
    expect(buttons[0]).toBeDisabled();
    // the second row's Dismiss is untouched — busy state didn't leak across rows
    expect(buttons[1]).toHaveTextContent(/^Dismiss$/);
    expect(buttons[1]).not.toBeDisabled();
  });
});
