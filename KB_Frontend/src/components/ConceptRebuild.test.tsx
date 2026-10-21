import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  fetchConsolidationStatus: vi.fn(),
  requestConsolidation: vi.fn(),
}));
vi.mock("../api/client", () => api);
const subscribeChanges = vi.fn();
vi.mock("../api/live", () => ({
  subscribeChanges: (cb: (e: { table: string }) => void) => subscribeChanges(cb),
}));

import { ConceptRebuild } from "./ConceptRebuild";

const idle = {
  state: "idle" as const,
  request: null,
  running: null,
  last: {
    run_id: 140,
    finished_at: "2026-08-03T09:19:29Z",
    status: "done",
    trigger_reason: "churn",
    error: null,
    communities: 27,
    kept: 23,
    created: 1,
    updated: 20,
    retired: 1,
    discarded: 2,
    label_failures: 0,
    embed_failures: 0,
    label_error: null,
    embed_error: null,
  },
  backlog: 0,
};

describe("ConceptRebuild", () => {
  beforeEach(() => {
    api.fetchConsolidationStatus.mockReset().mockResolvedValue(idle);
    api.requestConsolidation.mockReset().mockResolvedValue({ ...idle, state: "queued" });
    subscribeChanges.mockReset().mockReturnValue(() => {});
  });

  it("shows the last run and an enabled button when idle", async () => {
    render(<ConceptRebuild onFinished={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/23 concepts/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /rebuild concepts/i })).toBeEnabled();
  });

  it("requests a rebuild once per click and disables while queued", async () => {
    render(<ConceptRebuild onFinished={vi.fn()} />);
    await waitFor(() => expect(api.fetchConsolidationStatus).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /rebuild concepts/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /queued/i })).toBeDisabled());
    expect(api.requestConsolidation).toHaveBeenCalledTimes(1);
  });

  it("shows the phase while a pass runs", async () => {
    api.fetchConsolidationStatus.mockResolvedValue({
      ...idle,
      state: "running",
      running: {
        run_id: 141,
        started_at: "2026-08-03T10:42:00Z",
        phase: "labeling",
        trigger_reason: "manual",
      },
    });
    render(<ConceptRebuild onFinished={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/labeling/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /consolidating/i })).toBeDisabled();
  });

  it("says the daemon is not responding when stalled, and stays clickable", async () => {
    api.fetchConsolidationStatus.mockResolvedValue({
      ...idle,
      state: "stalled",
      request: {
        id: 3,
        requested_at: "2026-08-03T10:42:00Z",
        claimed_at: null,
        finished_at: null,
        error: null,
      },
    });
    render(<ConceptRebuild onFinished={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/not responding/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /rebuild concepts/i })).toBeEnabled();
  });

  it("warns when the enrichment backlog means a forced pass clusters early", async () => {
    api.fetchConsolidationStatus.mockResolvedValue({ ...idle, backlog: 12 });
    render(<ConceptRebuild onFinished={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(/12 memories still enriching/i)).toBeInTheDocument());
  });

  it("surfaces a failed last run", async () => {
    // A failed row's stats come from run_once's `except` — the whole-pass `error` is
    // set, but label_error/embed_error are only ever written on the success path, so
    // the backend can never produce a failed row with those populated instead.
    api.fetchConsolidationStatus.mockResolvedValue({
      ...idle,
      last: {
        ...idle.last,
        status: "failed",
        error: "apply boom",
        label_error: null,
        embed_error: null,
      },
    });
    render(<ConceptRebuild onFinished={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(/apply boom/)).toBeInTheDocument());
  });

  it("refetches when the change feed reports concept tables", async () => {
    render(<ConceptRebuild onFinished={vi.fn()} />);
    await waitFor(() => expect(api.fetchConsolidationStatus).toHaveBeenCalledTimes(1));
    const emit = subscribeChanges.mock.calls[0][0];
    emit({ table: "concept_runs" });
    await waitFor(() => expect(api.fetchConsolidationStatus).toHaveBeenCalledTimes(2));
  });

  it("calls onFinished when a run completes", async () => {
    const onFinished = vi.fn();
    api.fetchConsolidationStatus.mockResolvedValue({
      ...idle,
      state: "running",
      running: {
        run_id: 141,
        started_at: "2026-08-03T10:42:00Z",
        phase: "labeling",
        trigger_reason: "manual",
      },
    });
    render(<ConceptRebuild onFinished={onFinished} />);
    await waitFor(() => expect(screen.getByText(/labeling/)).toBeInTheDocument());
    api.fetchConsolidationStatus.mockResolvedValue(idle);
    const emit = subscribeChanges.mock.calls[0][0];
    emit({ table: "concept_runs" });
    await waitFor(() => expect(onFinished).toHaveBeenCalled());
  });
});
