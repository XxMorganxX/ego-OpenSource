import { describe, expect, it } from "vitest";

import type { GraphLink } from "../types";
import { assignRadialTargets, idJitter } from "./radial";

const link = (source: string, target: string): GraphLink => ({
  source,
  target,
  kind: "containment",
});
const n = (id: string, slug = id) => ({ id, slug });

describe("assignRadialTargets", () => {
  it("returns an empty map for an empty graph", () => {
    expect(assignRadialTargets([], [], 50).size).toBe(0);
  });

  it("puts a single root at the center and its children on ring 1", () => {
    const targets = assignRadialTargets(
      [n("root"), n("a"), n("b")],
      [link("root", "a"), link("root", "b")],
      50,
    );
    expect(targets.get("root")).toMatchObject({ depth: 0, r: 0 });
    expect(targets.get("a")).toMatchObject({ depth: 1, r: 50 });
    expect(targets.get("b")).toMatchObject({ depth: 1, r: 50 });
  });

  it("puts multiple roots on ring 1, leaving the center empty", () => {
    const targets = assignRadialTargets([n("a"), n("b")], [], 40);
    expect(targets.get("a")).toMatchObject({ depth: 1, r: 40 });
    expect(targets.get("b")).toMatchObject({ depth: 1, r: 40 });
  });

  it("assigns BFS depths independently per disconnected cluster", () => {
    const targets = assignRadialTargets(
      [n("a"), n("a1"), n("a11"), n("b")],
      [link("a", "a1"), link("a1", "a11")],
      10,
    );
    expect(targets.get("a")).toMatchObject({ depth: 1, r: 10 });
    expect(targets.get("a1")).toMatchObject({ depth: 2, r: 20 });
    expect(targets.get("a11")).toMatchObject({ depth: 3, r: 30 });
    expect(targets.get("b")).toMatchObject({ depth: 1, r: 10 });
  });

  it("sizes wedges proportional to subtree node count", () => {
    // heavy subtree = 3 nodes, light = 1 → heavy owns 3/4 of the circle.
    // Slug order: "heavy" < "light" → heavy [0, 1.5π) mid 0.75π; light [1.5π, 2π) mid 1.75π.
    const targets = assignRadialTargets(
      [n("heavy"), n("h1"), n("h2"), n("light")],
      [link("heavy", "h1"), link("heavy", "h2")],
      10,
    );
    expect(targets.get("heavy")!.theta).toBeCloseTo(0.75 * Math.PI);
    expect(targets.get("light")!.theta).toBeCloseTo(1.75 * Math.PI);
  });

  it("partitions a parent's wedge among children in slug order", () => {
    // Single root: children split the full circle. a → [0, π) mid π/2; b → [π, 2π) mid 1.5π.
    const targets = assignRadialTargets(
      [n("root"), n("b"), n("a")],
      [link("root", "a"), link("root", "b")],
      10,
    );
    expect(targets.get("a")!.theta).toBeCloseTo(Math.PI / 2);
    expect(targets.get("b")!.theta).toBeCloseTo(1.5 * Math.PI);
  });

  it("is deterministic under input reordering", () => {
    const t1 = assignRadialTargets([n("b"), n("a"), n("c")], [], 10);
    const t2 = assignRadialTargets([n("c"), n("b"), n("a")], [], 10);
    for (const id of ["a", "b", "c"]) expect(t1.get(id)).toEqual(t2.get(id));
  });

  it("files a node under its first containment parent when it has two", () => {
    const targets = assignRadialTargets(
      [n("p1"), n("p2"), n("kid")],
      [link("p1", "kid"), link("p2", "kid")],
      10,
    );
    // kid is filed under p1 (first link wins); p1 and p2 are both roots on ring 1.
    expect(targets.get("kid")).toMatchObject({ depth: 2, r: 20 });
    // As p1's only child, kid inherits p1's whole wedge — same midpoint angle.
    expect(targets.get("kid")!.theta).toBeCloseTo(targets.get("p1")!.theta);
    expect(targets.get("p2")).toMatchObject({ depth: 1, r: 10 });
  });

  it("falls back to id ordering for nodes without slugs", () => {
    const targets = assignRadialTargets([{ id: "b" }, { id: "a" }], [], 10);
    // Sorted by id: a → [0, π) mid π/2; b → [π, 2π) mid 1.5π.
    expect(targets.get("a")!.theta).toBeCloseTo(Math.PI / 2);
    expect(targets.get("b")!.theta).toBeCloseTo(1.5 * Math.PI);
  });

  it("ignores non-containment links and links to absent nodes", () => {
    const targets = assignRadialTargets(
      [n("a"), n("b")],
      [{ source: "a", target: "b", kind: "edge" }, link("a", "ghost")],
      10,
    );
    // The "edge" link must not make b a child of a — both stay roots on ring 1.
    expect(targets.get("a")).toMatchObject({ depth: 1 });
    expect(targets.get("b")).toMatchObject({ depth: 1 });
  });
});

describe("idJitter", () => {
  it("is deterministic and bounded to ±2 per axis", () => {
    const j1 = idJitter("some-node-id");
    const j2 = idJitter("some-node-id");
    expect(j1).toEqual(j2);
    expect(Math.abs(j1.dx)).toBeLessThanOrEqual(2);
    expect(Math.abs(j1.dy)).toBeLessThanOrEqual(2);
  });

  it("differs across different ids", () => {
    expect(idJitter("aaaa")).not.toEqual(idJitter("bbbb"));
  });
});
