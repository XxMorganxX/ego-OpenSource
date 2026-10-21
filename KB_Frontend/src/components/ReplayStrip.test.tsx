import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { LABELS } from "../test/replayFixtures";
import { ReplayStrip } from "./ReplayStrip";

const rows = [
  { id: "b", rank: 1, chips: ["kw#2", "mean#1"], cut: false },
  { id: "a", rank: 2, chips: ["kw#1"], cut: false },
  { id: "y", rank: 3, chips: [], cut: true },
];

describe("ReplayStrip", () => {
  it("renders ranked rows with chips, the limit line and cut rows", () => {
    const { container } = render(
      <ReplayStrip rows={rows} labels={LABELS} limitLine={2} motion={false}
                   onSelect={vi.fn()} onHover={vi.fn()} />);
    expect(screen.getByText("Beta")).toBeInTheDocument();
    expect(screen.getByText("mean#1")).toBeInTheDocument();
    expect(screen.getByText("── limit 2 ──")).toBeInTheDocument();
    expect(container.querySelectorAll(".replay-strip-row.cut")).toHaveLength(1);
  });

  it("selects any row, deleted ones too -- their path can still be traced", () => {
    const onSelect = vi.fn();
    render(<ReplayStrip rows={rows} labels={LABELS} limitLine={null} motion={false}
                        onSelect={onSelect} onHover={vi.fn()} />);
    fireEvent.click(screen.getByText("Beta"));
    fireEvent.click(screen.getByText("(deleted)"));
    expect(onSelect.mock.calls).toEqual([["b"], ["y"]]);
  });

  it("marks the pinned row", () => {
    const { container } = render(<ReplayStrip rows={rows} labels={LABELS} limitLine={null} motion={false}
                                              pinned="b" onSelect={vi.fn()} onHover={vi.fn()} />);
    expect(container.querySelectorAll(".replay-strip-row.pinned")).toHaveLength(1);
  });

  it("says so when a stage has no ranked list", () => {
    render(<ReplayStrip rows={[]} labels={LABELS} limitLine={null} motion={false}
                        onSelect={vi.fn()} onHover={vi.fn()} />);
    expect(screen.getByText("No ranked list at this stage.")).toBeInTheDocument();
  });
});
