import { describe, expect, it } from "vitest";

import { placeGraphLabels, type GraphLabel } from "./graphLabels";

const overlap = (a: GraphLabel["box"], b: GraphLabel["box"]) =>
  a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;

function expectNoOverlaps(labels: GraphLabel[]) {
  for (let i = 0; i < labels.length; i++)
    for (let j = i + 1; j < labels.length; j++)
      expect(overlap(labels[i].box, labels[j].box), `${labels[i].id} × ${labels[j].id}`).toBe(false);
}

const view = { left: -500, top: -500, right: 500, bottom: 500 };

describe("placeGraphLabels", () => {
  it("places well-separated labels to the right of their nodes", () => {
    const labels = placeGraphLabels(
      [
        { id: "a", x: -200, y: 0, r: 5, text: "alpha · 10" },
        { id: "b", x: 200, y: 0, r: 5, text: "beta · 3" },
      ],
      [],
      1,
      view,
    );
    expect(labels.map((l) => [l.id, l.anchor])).toEqual([["a", "start"], ["b", "start"]]);
    expect(labels[0].x).toBeGreaterThan(-200 + 5); // clear of its own node
  });

  it("never overlaps two labels — a crowded one moves to another side or is dropped", () => {
    // Four labels within a few px of each other.
    const crowd = [
      { id: "alpha", x: 120, y: 30, r: 10, text: "alpha · 300" },
      { id: "beta", x: 135, y: 22, r: 8, text: "beta · 120" },
      { id: "gamma", x: 128, y: 45, r: 8, text: "gamma · 170" },
      { id: "delta", x: 150, y: 52, r: 8, text: "delta · 200" },
    ];
    const labels = placeGraphLabels(crowd, [], 1, view);
    expectNoOverlaps(labels);
    expect(labels[0].id).toBe("alpha"); // highest priority is always placed
    expect(labels.length).toBeGreaterThanOrEqual(2);
  });

  it("gives earlier items priority", () => {
    const same = [
      { id: "first", x: 0, y: 0, r: 4, text: "first · 9" },
      { id: "second", x: 0, y: 0, r: 4, text: "second · 1" },
    ];
    const [a, b] = placeGraphLabels(same, [], 1, view);
    expect(a.id).toBe("first");
    expect(a.anchor).toBe("start"); // first takes the preferred right-hand spot
    expect(b?.anchor).toBe("end"); // second falls back to the left
  });

  it("keeps labels off other node circles", () => {
    // A big node sits exactly where "a"'s right-hand label would go.
    const labels = placeGraphLabels(
      [{ id: "a", x: 0, y: 0, r: 4, text: "a · 5" }],
      [{ x: 30, y: 0, r: 20 }],
      1,
      view,
    );
    expect(labels[0].anchor).toBe("end");
  });

  it("reveals crowded labels as you zoom in", () => {
    const crowd = Array.from({ length: 6 }, (_, i) => ({
      id: `n${i}`, x: i * 6, y: (i % 2) * 6, r: 2, text: `folder${i} · ${i}`,
    }));
    const zoomedOut = placeGraphLabels(crowd, [], 0.5, view);
    const zoomedIn = placeGraphLabels(crowd, [], 8, view);
    expectNoOverlaps(zoomedOut);
    expectNoOverlaps(zoomedIn);
    expect(zoomedIn.length).toBeGreaterThan(zoomedOut.length);
    expect(zoomedIn.length).toBe(6);
  });

  it("works without a captured view rect (first frame)", () => {
    const labels = placeGraphLabels([{ id: "a", x: 5000, y: -5000, r: 4, text: "far · 1" }], [], 1, null);
    expect(labels.map((l) => l.id)).toEqual(["a"]);
  });
});
