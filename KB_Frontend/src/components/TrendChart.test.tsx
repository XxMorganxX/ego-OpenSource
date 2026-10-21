import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TrendChart, type TrendSeries } from "./TrendChart";

const series: TrendSeries[] = [
  {
    label: "must_recall@20",
    color: "#4f8cff",
    points: [
      { x: 0, y: 0.6, label: "2025-03-01" },
      { x: 1, y: 0.8, label: "2025-03-15" },
    ],
  },
  {
    label: "mrr",
    color: "#f2a447",
    points: [{ x: 0, y: 0.5, label: "2025-03-01" }],
  },
];

describe("TrendChart", () => {
  it("renders one polyline per series and a legend entry each", () => {
    const { container } = render(<TrendChart series={series} />);
    expect(container.querySelectorAll("polyline")).toHaveLength(2);
    expect(screen.getByText("must_recall@20")).toBeInTheDocument();
    expect(screen.getByText("mrr")).toBeInTheDocument();
  });

  it("labels every point with an accessible title", () => {
    const { container } = render(<TrendChart series={series} />);
    const titles = [...container.querySelectorAll("circle > title")].map(
      (t) => t.textContent,
    );
    expect(titles).toContain("2025-03-01 · must_recall@20 0.6");
  });

  it("renders an empty state when there are no points", () => {
    render(<TrendChart series={[]} />);
    expect(screen.getByText("No runs yet.")).toBeInTheDocument();
  });

  it("survives yMax=0 without NaN coordinates", () => {
    const { container } = render(<TrendChart series={series} yMax={0} />);
    const cys = [...container.querySelectorAll("circle")].map((c) =>
      Number(c.getAttribute("cy")),
    );
    expect(cys.length).toBeGreaterThan(0);
    for (const cy of cys) expect(Number.isFinite(cy)).toBe(true);
  });
});
