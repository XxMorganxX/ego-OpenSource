import { describe, expect, it } from "vitest";

import { CRAWL, HYBRID, PARTIAL } from "../test/replayFixtures";
import type { ReplayRecord } from "../types";
import { NOT_RECORDED, buildStages, filterSummary } from "./replayStages";

const byId = (rec: ReplayRecord, id: string, args = {}) => {
  const s = buildStages(rec, args).find((x) => x.id === id);
  if (!s) throw new Error(`no stage ${id}`);
  return s;
};

describe("buildStages", () => {
  it("search_memories plays query → arms → merge → cut", () => {
    const s = buildStages(HYBRID, { query: "postgres" });
    expect(s.map((x) => x.id)).toEqual(["query", "keyword", "meaning", "merge", "cut"]);
    expect(s.every((x) => x.available)).toBe(true);
  });

  it("search_graph adds entry, one stage per hop, and the final merge", () => {
    expect(buildStages(CRAWL, {}).map((x) => x.id)).toEqual(
      ["query", "keyword", "meaning", "merge", "entry", "hop", "fusion", "cut"]);
  });

  it("reveals nodes and edges cumulatively", () => {
    const kw = byId(HYBRID, "keyword");
    expect([...kw.nodes].sort()).toEqual(["a", "b", "query"]);
    expect(kw.edges.has("fts:a")).toBe(true);
    const mean = byId(HYBRID, "meaning");
    expect(mean.nodes.has("a") && mean.nodes.has("c")).toBe(true);
    expect(mean.edges.has("fts:a") && mean.edges.has("knn:c")).toBe(true);
  });

  it("merge highlights rows both arms found and chips their arm positions", () => {
    const m = byId(HYBRID, "merge");
    expect([...m.highlight]).toEqual(["b"]);
    expect(m.strip[0]).toEqual({ id: "b", rank: 1, chips: ["kw#2", "mean#1"], cut: false });
  });

  it("cut dims everything below the limit and draws the limit line", () => {
    const c = byId(HYBRID, "cut");
    expect(c.strip.map((r) => r.cut)).toEqual([false, false, true]);
    expect(c.dimmed.has("c")).toBe(true);
    expect(c.dimmed.has("query")).toBe(false);
    expect(c.limitLine).toBe(2);
    expect(c.caption).toBe("top 2: 1 both · 1 keyword only · 1 cut");
  });

  it("a hop shows offered rows round their hub and dims the rejected ones", () => {
    const h = byId(CRAWL, "hop");
    expect(h.nodes.has("x") && h.nodes.has("y") && h.nodes.has("hub:k1")).toBe(true);
    expect(h.edges.has("parent:k1:x")).toBe(true);
    expect([...h.dimmed]).toEqual(["y"]);
    expect(h.caption).toBe("2 offered → 1 admitted (deterministic: first 1 of 2 offered)");
  });

  it("the final merge names the four lists and why admit is empty", () => {
    expect(byId(CRAWL, "fusion").caption).toBe(
      "lists: flat 3 · sim 3 · admit 0 (empty: deterministic judge) · corrob 0");
    expect(byId(CRAWL, "fusion").strip.find((r) => r.id === "x")!.chips).toEqual(["crawl"]);
  });

  it("a partial replay greys the stages it never recorded", () => {
    const s = buildStages(PARTIAL, { query: "alpha" });
    const kw = s.find((x) => x.id === "keyword")!;
    expect(kw.available).toBe(false);
    expect(kw.caption).toBe(NOT_RECORDED);
    expect(s.find((x) => x.id === "merge")!.available).toBe(false);
    const cut = s.find((x) => x.id === "cut")!;
    expect(cut.available).toBe(true);
    expect(cut.strip.map((r) => r.id)).toEqual(["a", "b"]);
    expect(cut.nodes.has("a")).toBe(true);
  });

  it("a crawl with no entry seeds skips hop and final merge", () => {
    const rec: ReplayRecord = { ...CRAWL, crawl: { ...CRAWL.crawl!, entry: [], hops: [] } };
    const s = buildStages(rec, {});
    expect(s.find((x) => x.id === "entry")!.caption).toBe("no concept/entity route — flat only");
    expect(s.find((x) => x.id === "hop")!.available).toBe(false);
    expect(s.find((x) => x.id === "fusion")!.available).toBe(false);
  });

  it("says when the embedder was down", () => {
    const rec: ReplayRecord = { ...HYBRID, flat: { ...HYBRID.flat, knn: [], embedder: "unavailable" } };
    expect(byId(rec, "meaning").caption).toBe("meaning search skipped: embedder unavailable");
  });

  it("the query stage names the searched text, the any-word fallback and filters", () => {
    const rec: ReplayRecord = { ...HYBRID, flat: { ...HYBRID.flat, fts_query: "a OR b", or_fallback: true } };
    expect(byId(rec, "query", { query: "a b", under: "proj", include_dead: false }).caption).toBe(
      'searched "a OR b" · any-word fallback · filters: under=proj, include_dead=false');
  });

  it("a partial hop keeps its counts but says its rows were not recorded", () => {
    const rec: ReplayRecord = { ...CRAWL, crawl: { ...CRAWL.crawl!, hops: [
      { hop: 1, by: "none", note: null, offered_count: 40, admitted_count: 12 }] } };
    const h = buildStages(rec, {}).find((x) => x.id === "hop")!;
    expect(h.available).toBe(true);
    expect(h.caption).toBe("40 offered → 12 admitted · rows not recorded (traced before replay capture)");
  });

  it("rows rejected in an earlier hop stay dimmed in later hops", () => {
    const hop2 = { hop: 2, by: "none", note: null, offered_count: 1, admitted_count: 1,
      offered: [{ id: "z", family: "bridge" as const, admitted: true,
                  parents: [{ kind: "entity" as const, id: "e1", label: "E" }] }] };
    const rec: ReplayRecord = { ...CRAWL, crawl: { ...CRAWL.crawl!, hops: [...CRAWL.crawl!.hops, hop2] } };
    const hops = buildStages(rec, {}).filter((x) => x.id === "hop");
    expect(hops.map((h) => h.key)).toEqual(["hop-1", "hop-2"]);
    expect(hops[1].dimmed.has("y")).toBe(true);
    expect(hops[1].nodes.has("hub:e1")).toBe(true);
  });

  it("filterSummary joins list-valued filters", () => {
    expect(filterSummary({ tags: ["db", "perf"], kind: null })).toBe("tags=db,perf");
  });
});
