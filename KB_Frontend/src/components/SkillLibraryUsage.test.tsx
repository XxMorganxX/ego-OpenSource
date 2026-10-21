import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SkillLibraryUsageData } from "../types";
import { SkillLibraryUsage } from "./SkillLibraryUsage";

const usage: SkillLibraryUsageData = {
  days: 7, tz: "UTC", since: "2026-10-12T00:00:00+00:00",
  searches_per_day: [
    { day: "2026-10-17", searches: 5, missed: 1 },
    { day: "2026-10-18", searches: 4, missed: 2 },
  ],
  missed_searches: [{ query: "deploy checklist", count: 3, last_at: "2026-10-18T09:00:00+00:00" }],
  missing_requests: [{ name: "release-notes", count: 2, last_at: "2026-10-18T09:00:00+00:00" }],
  per_skill: {}, never_loaded: ["beta-skill"], surfaced_since: null,
};

const base = { error: null, days: 7 as const, onDaysChange: vi.fn(), onSelectSkill: vi.fn() };

describe("SkillLibraryUsage", () => {
  it("totals the window and lists misses, missing skills and never-loaded", () => {
    render(<SkillLibraryUsage {...base} usage={usage} />);
    expect(screen.getByText("9")).toBeInTheDocument();          // searches
    expect(screen.getByText("“deploy checklist”")).toBeInTheDocument();
    expect(screen.getByText("release-notes")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "beta-skill" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Searches per day over 2 days" })).toBeInTheDocument();
  });

  it("ranks the most loaded skills and opens one", async () => {
    const onSelectSkill = vi.fn();
    render(<SkillLibraryUsage {...base} onSelectSkill={onSelectSkill} usage={{ ...usage, per_skill: {
      "alpha-skill": { loads: 3, last_loaded_at: "2026-10-18T09:00:00+00:00" },
      "gamma-skill": { loads: 11, last_loaded_at: "2026-10-18T09:00:00+00:00" },
      "idle-skill": { loads: 0, last_loaded_at: "2026-10-18T09:00:00+00:00" },
    } }} />);
    const card = screen.getByText("Most loaded").closest("div.skills-overview-card") as HTMLElement;
    expect(within(card).getAllByRole("button").map((b) => b.textContent)).toEqual(["gamma-skill", "alpha-skill"]);
    await userEvent.click(within(card).getByRole("button", { name: "gamma-skill" }));
    expect(onSelectSkill).toHaveBeenCalledWith("gamma-skill");
  });

  it("opens a never-loaded skill and changes the window", async () => {
    const onSelectSkill = vi.fn();
    const onDaysChange = vi.fn();
    render(<SkillLibraryUsage {...base} usage={usage} onSelectSkill={onSelectSkill}
                              onDaysChange={onDaysChange} />);
    await userEvent.click(screen.getByRole("button", { name: "beta-skill" }));
    expect(onSelectSkill).toHaveBeenCalledWith("beta-skill");
    await userEvent.selectOptions(screen.getByLabelText("Library usage window"), "30");
    expect(onDaysChange).toHaveBeenCalledWith(30);
  });

  it("shows loading, error and empty states", () => {
    const { rerender } = render(<SkillLibraryUsage {...base} usage={null} />);
    expect(screen.getByText("Loading usage…")).toBeInTheDocument();
    rerender(<SkillLibraryUsage {...base} usage={null} error="Failed to load usage." />);
    expect(screen.getByRole("alert")).toHaveTextContent("Failed to load usage.");
    rerender(<SkillLibraryUsage {...base} usage={{ ...usage, searches_per_day: [],
      missed_searches: [], missing_requests: [], never_loaded: [] }} />);
    expect(screen.getByText("No skill activity in the last 7 days")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    rerender(<SkillLibraryUsage {...base} libraryEmpty usage={{ ...usage, searches_per_day: [],
      missed_searches: [], missing_requests: [], never_loaded: [] }} />);
    expect(screen.getByText("No active skills in the library")).toBeInTheDocument();
    expect(screen.queryByText(/No skill activity/)).toBeNull();
    rerender(<SkillLibraryUsage {...base} usage={{ ...usage, missed_searches: [], missing_requests: [] }} />);
    expect(screen.getByText("No missed searches.")).toBeInTheDocument();
  });
});
