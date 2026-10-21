import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { niceTicks, SkillSearchChart } from "./SkillSearchChart";

describe("SkillSearchChart", () => {
  it("picks clean ticks that cover the maximum", () => {
    expect(niceTicks(5)).toEqual([0, 2, 4, 6]);
    expect(niceTicks(17)).toEqual([0, 5, 10, 15, 20]);
    expect(niceTicks(0)).toEqual([0, 1]);
    // A quiet window: whole searches only, never 0.5.
    expect(niceTicks(1)).toEqual([0, 1]);
    expect(niceTicks(2)).toEqual([0, 1, 2]);
    expect(niceTicks(3)).toEqual([0, 1, 2, 3]);
  });

  it("draws one found and one missed segment per day that has them", () => {
    const { container } = render(<SkillSearchChart days={[
      { day: "2026-10-17", searches: 5, missed: 1 },
      { day: "2026-10-18", searches: 4, missed: 0 },
    ]} />);
    expect(screen.getByRole("img", { name: "Searches per day over 2 days" })).toBeInTheDocument();
    expect(container.querySelectorAll(".skills-chart-found")).toHaveLength(2);
    expect(container.querySelectorAll(".skills-chart-missed")).toHaveLength(1);
  });
});
