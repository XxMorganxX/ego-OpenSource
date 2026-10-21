import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchIngestStatus = vi.fn();
const subscribeChanges = vi.fn();
vi.mock("../api/client", () => ({
  fetchIngestStatus: () => fetchIngestStatus(),
}));
vi.mock("../api/live", () => ({
  subscribeChanges: (cb: () => void) => subscribeChanges(cb),
}));

import { ReenrichBanner } from "./ReenrichBanner";

describe("ReenrichBanner", () => {
  beforeEach(() => {
    fetchIngestStatus.mockReset();
    subscribeChanges.mockReset().mockReturnValue(() => {});
  });

  it("shows the pending re-enrichment count", async () => {
    fetchIngestStatus.mockResolvedValue({ enrichment: { pending: 3, done: 10 }, sources: [] });
    render(<ReenrichBanner />);
    await waitFor(() => expect(screen.getByText(/3 memories re-enriching/i)).toBeInTheDocument());
  });

  it("renders nothing when nothing is pending", async () => {
    fetchIngestStatus.mockResolvedValue({ enrichment: { pending: 0, done: 10 }, sources: [] });
    const { container } = render(<ReenrichBanner />);
    await waitFor(() => expect(fetchIngestStatus).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });
});
