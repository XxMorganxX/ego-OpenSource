import { describe, expect, it } from "vitest";

import { CALLOUT_X, CALLOUT_ROW, placeCallouts } from "./replayCallouts";
import { CENTER, R_RING, VIEW_H, VIEW_W } from "./replayLayout";
import { MAX_CHARS } from "./replayLabels";

const rows = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `m${i}`, rank: i + 1 }));
const pos = (n: number) => new Map(rows(n).map((r, i) => [r.id, { x: 300 + i, y: 100 + i * 10 }]));
const title = (id: string) => `Memory ${id}`;

describe("placeCallouts", () => {
  it("lists the top rows in rank order, top to bottom, one row apart", () => {
    const { callouts, more } = placeCallouts(rows(3), pos(3), title, 10);
    expect(callouts.map((c) => c.text)).toEqual(["1 Memory m0", "2 Memory m1", "3 Memory m2"]);
    expect(callouts[1].y - callouts[0].y).toBe(CALLOUT_ROW);
    expect(callouts.every((c) => c.x === CALLOUT_X)).toBe(true);
    expect(more).toBe(0);
  });

  it("stops at the limit", () => {
    const { callouts } = placeCallouts(rows(12), pos(12), title, 10);
    expect(callouts).toHaveLength(10);
  });

  it("keeps a long column inside the view and counts what did not fit", () => {
    const { callouts, more } = placeCallouts(rows(60), pos(60), title, 50);
    expect(callouts.length).toBeGreaterThan(20);
    expect(callouts.length + more).toBe(50);
    for (const c of callouts) {
      expect(c.y).toBeGreaterThan(0);
      expect(c.y).toBeLessThan(VIEW_H);
    }
  });

  it("centres a short column on the graph", () => {
    const { callouts } = placeCallouts(rows(1), pos(1), title, 10);
    expect(Math.abs(callouts[0].y - CENTER.y)).toBeLessThan(CALLOUT_ROW);
  });

  it("truncates long titles so the column stays inside the view", () => {
    const { callouts } = placeCallouts(rows(1), pos(1), () => "x".repeat(80), 10);
    expect(callouts[0].text.length).toBe(MAX_CHARS);
    expect(CALLOUT_X + MAX_CHARS * 6.8).toBeLessThanOrEqual(VIEW_W);
  });

  it("sits clear of the graph ring", () => {
    expect(CALLOUT_X).toBeGreaterThan(CENTER.x + R_RING);
  });

  it("skips a row with no node to point at", () => {
    const { callouts } = placeCallouts(rows(2), new Map([["m1", { x: 1, y: 1 }]]), title, 10);
    expect(callouts.map((c) => c.id)).toEqual(["m1"]);
  });
});
