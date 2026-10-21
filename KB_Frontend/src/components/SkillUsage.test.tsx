import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SkillUsageData } from "../types";

const fetchSkillUsage = vi.fn();
vi.mock("../api/client", () => ({
  fetchSkillUsage: (...a: unknown[]) => fetchSkillUsage(...a),
}));

import { SkillUsage } from "./SkillUsage";

const usage = (over: Partial<SkillUsageData> = {}): SkillUsageData => ({
  name: "deploy-checklist", days: 30, tz: "UTC", since: "2026-09-19T00:00:00+00:00",
  loads: 12, file_reads: 4, surfaced: 31, last_loaded_at: "2026-10-17T14:02:11+00:00",
  surfaced_since: "2026-09-01T00:00:00+00:00",
  recent_loads: [
    { at: "2026-10-17T14:02:11+00:00", revision: 3, session_id: "s-1", turn_id: 9123 },
    { at: "2026-10-16T09:00:00+00:00", revision: null, session_id: null, turn_id: null },
  ],
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/?view=skills&skill=deploy-checklist");
  fetchSkillUsage.mockResolvedValue(usage());
});

describe("SkillUsage", () => {
  it("shows counts and recent loads", async () => {
    render(<SkillUsage name="deploy-checklist" refreshTick={0} />);
    expect(await screen.findByText("surfaced in 31 searches")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("rev 3")).toBeInTheDocument();
    expect(screen.getByText("current (not recorded)")).toBeInTheDocument();
    expect(fetchSkillUsage).toHaveBeenCalledWith("deploy-checklist", 30, expect.any(String));
  });

  it("changes the window", async () => {
    render(<SkillUsage name="deploy-checklist" refreshTick={0} />);
    await screen.findByText("surfaced in 31 searches");
    await userEvent.selectOptions(screen.getByLabelText("Usage window"), "7");
    await waitFor(() =>
      expect(fetchSkillUsage).toHaveBeenLastCalledWith("deploy-checklist", 7, expect.any(String)));
  });

  it("links a load to its request on the Retrieval page", async () => {
    render(<SkillUsage name="deploy-checklist" refreshTick={0} />);
    await userEvent.click(await screen.findByRole("link", { name: "request #9123" }));
    const params = new URLSearchParams(window.location.search);
    expect(params.get("view")).toBe("retrieval");
    expect(params.get("turn")).toBe("9123");
    expect(params.get("skill")).toBeNull();
  });

  it("says when there were no loads", async () => {
    fetchSkillUsage.mockResolvedValue(usage({ loads: 0, recent_loads: [], last_loaded_at: null }));
    render(<SkillUsage name="deploy-checklist" refreshTick={0} />);
    expect(await screen.findByText("No loads in the last 30 days.")).toBeInTheDocument();
  });
});
