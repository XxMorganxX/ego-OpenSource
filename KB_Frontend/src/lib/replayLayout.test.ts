import { describe, expect, it } from "vitest";

import { CRAWL, HYBRID, PARTIAL } from "../test/replayFixtures";
import type { ReplayRecord } from "../types";
import { CENTER, R_RING, buildLayout, hubId, radiusFor } from "./replayLayout";

const at = (rec: ReplayRecord, id: string) => {
  const n = buildLayout(rec).nodes.find((m) => m.id === id);
  if (!n) throw new Error(`no node ${id}`);
  return n;
};
const dist = (p: { x: number; y: number }) => Math.hypot(p.x - CENTER.x, p.y - CENTER.y);

describe("buildLayout", () => {
  it("is deterministic", () => {
    expect(buildLayout(CRAWL)).toEqual(buildLayout(CRAWL));
  });

  it("puts each memory in the sector of the arms that found it", () => {
    expect(at(CRAWL, "a").x).toBeLessThan(CENTER.x);     // keyword-only: left
    expect(at(CRAWL, "c").x).toBeGreaterThan(CENTER.x);  // meaning-only: right
    expect(at(CRAWL, "b").y).toBeLessThan(CENTER.y);     // both: top
    expect(at(CRAWL, "x").y).toBeGreaterThan(CENTER.y);  // crawl: bottom
    expect(at(CRAWL, "a").sector).toBe("kw");
    expect(at(CRAWL, "x").sector).toBe("crawl");
  });

  it("places more similar memories closer to the query", () => {
    const rec: ReplayRecord = { v: 1, limit: 5, flat: {
      fts: [], knn: [{ id: "near", sim: 0.9 }, { id: "far", sim: 0.5 }],
      fused: [{ id: "near" }, { id: "far" }] } };
    expect(dist(at(rec, "near"))).toBeLessThan(dist(at(rec, "far")));
    expect(radiusFor(0.9)).toBeLessThan(radiusFor(0.5));
  });

  it("puts a memory with no similarity on the outer ring", () => {
    expect(dist(at(HYBRID, "a"))).toBeCloseTo(R_RING, 5);
  });

  it("emits arm, entry and parent edges keyed as the stages expect", () => {
    const ids = buildLayout(CRAWL).edges.map((e) => e.id);
    expect(ids).toEqual(expect.arrayContaining(
      ["fts:a", "fts:b", "knn:b", "knn:c", "entry:k1", "parent:k1:x", "parent:k1:y"]));
    expect(buildLayout(CRAWL).nodes.map((n) => n.id)).toContain(hubId("k1"));
  });

  it("gives a hop-2 bridge parent that is not an entry seed its own hub, edge and cluster", () => {
    const hop2 = { hop: 2, by: "none", note: null, offered_count: 1, admitted_count: 1,
      offered: [{ id: "z", family: "bridge" as const, admitted: true,
                  parents: [{ kind: "entity" as const, id: "e1", label: null }] }] };
    const rec: ReplayRecord = { ...CRAWL, crawl: { ...CRAWL.crawl!, hops: [...CRAWL.crawl!.hops, hop2] } };
    const l = buildLayout(rec);
    const hub = l.nodes.find((n) => n.id === "hub:e1")!;
    expect(hub).toBeDefined();
    expect(l.edges.map((e) => e.id)).toContain("parent:e1:z");
    expect(l.edges.map((e) => e.id)).not.toContain("entry:e1");
    const z = l.nodes.find((n) => n.id === "z")!;
    expect(Math.hypot(z.x - hub.x, z.y - hub.y)).toBeLessThanOrEqual(46 + 14);
  });

  it("lays out a partial replay without arms", () => {
    const l = buildLayout(PARTIAL);
    expect(l.nodes.map((n) => n.id).sort()).toEqual(["a", "b", "query"]);
    expect(l.nodes.find((n) => n.id === "a")!.sector).toBe("unknown");
  });

  it("keeps every hub between the keyword and meaning sectors", () => {
    const many: ReplayRecord = { ...CRAWL, crawl: { ...CRAWL.crawl!, entry: Array.from({ length: 8 },
      (_, i) => ({ kind: "concept" as const, id: `k${i}`, label: `K${i}`, via: "label-cosine", rank: i + 1 })) } };
    for (const n of buildLayout(many).nodes.filter((x) => x.kind === "hub")) {
      const deg = (Math.atan2(-(n.y - CENTER.y), n.x - CENTER.x) * 180) / Math.PI + 360;
      expect(deg % 360).toBeGreaterThan(230);   // clear of the keyword sector (130–230)
      expect(deg % 360).toBeLessThan(310);      // clear of the meaning sector (−50 = 310)
    }
  });
});

