import { describe, expect, it } from "vitest";

import { MAX_CHARS, labelBox, placeLabels, truncate, type LabelRequest } from "./replayLabels";

const BOUNDS = { width: 1000, height: 700 };
const req = (id: string, x: number, y: number, text = `label ${id}`): LabelRequest =>
  ({ id, x, y, r: 6, text });

const overlaps = (a: ReturnType<typeof labelBox>, b: ReturnType<typeof labelBox>) =>
  a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;

describe("truncate", () => {
  it("keeps short text and ellipsizes long text to MAX_CHARS", () => {
    expect(truncate("short")).toBe("short");
    const long = "x".repeat(MAX_CHARS + 10);
    expect(truncate(long)).toHaveLength(MAX_CHARS);
    expect(truncate(long).endsWith("…")).toBe(true);
  });
});

describe("placeLabels", () => {
  it("never places two labels whose boxes overlap", () => {
    const reqs = Array.from({ length: 12 }, (_, i) => req(`n${i}`, 500 + (i % 3) * 4, 300 + i * 3));
    const placed = [...placeLabels(reqs, BOUNDS).values()];
    expect(placed.length).toBeGreaterThan(1);
    for (let i = 0; i < placed.length; i++)
      for (let j = i + 1; j < placed.length; j++)
        expect(overlaps(labelBox(placed[i]), labelBox(placed[j]))).toBe(false);
  });

  it("gives the first (highest-priority) label its preferred spot, right of the node", () => {
    const placed = placeLabels([req("a", 500, 300), req("b", 500, 300)], BOUNDS);
    expect(placed.get("a")).toMatchObject({ anchor: "start", y: 304 });
    expect(placed.get("a")!.x).toBeGreaterThan(500);
    expect(placed.get("b")).toBeDefined();
    expect(overlaps(labelBox(placed.get("a")!), labelBox(placed.get("b")!))).toBe(false);
  });

  it("drops a label that has no free spot", () => {
    const reqs = Array.from({ length: 10 }, (_, i) => req(`n${i}`, 50, 20));
    const placed = placeLabels(reqs, { width: 120, height: 60 });
    expect(placed.size).toBeLessThan(10);
    expect(placed.has("n0")).toBe(true);
  });

  it("flips to the left of a node near the right edge", () => {
    const placed = placeLabels([req("edge", 990, 300, "a fairly long label")], BOUNDS);
    expect(placed.get("edge")).toMatchObject({ anchor: "end" });
    expect(labelBox(placed.get("edge")!).x2).toBeLessThanOrEqual(1000);
  });

  it("keeps clear of reserved boxes", () => {
    const reserved = [{ x1: 500, y1: 290, x2: 700, y2: 310 }];
    const p = placeLabels([req("a", 495, 300)], BOUNDS, reserved).get("a")!;
    expect(overlaps(labelBox(p), reserved[0])).toBe(false);
  });

  it("truncates what it places", () => {
    const p = placeLabels([req("a", 100, 100, "y".repeat(80))], BOUNDS).get("a")!;
    expect(p.text).toHaveLength(MAX_CHARS);
  });
});
