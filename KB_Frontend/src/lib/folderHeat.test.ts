import { describe, expect, it } from "vitest";

import type { GraphData, NodeMeta } from "../types";
import {
  COLD_LINK,
  folderHeat,
  folderSubgraph,
  hotFolderGraph,
  viewRoots,
  heatLinkColor,
  heatLinkWidth,
  heatRadius,
  HEAT_R_COLD,
  HEAT_R_MAX,
  HEAT_R_MIN,
} from "./folderHeat";
import { heatFraction } from "./heat";

const mem = (id: string, path: string, surfaced: number, opened = 0, title: string | null = null) => ({
  id, path, title, surfaced, opened,
});

describe("folderHeat", () => {
  const folders = [
    { id: "f-a", path: "a" },
    { id: "f-ab", path: "a.b" },
    { id: "f-abc", path: "a.bc" },
    { id: "f-z", path: "z" },
  ];

  it("sums every memory beneath a folder, at any depth", () => {
    const { heat } = folderHeat(folders, [
      mem("1", "a.b.note1", 5, 1),
      mem("2", "a.b.deep.note2", 3),
      mem("3", "a.note3", 2),
    ]);
    expect(heat.get("f-ab")).toMatchObject({ total: 9, opened: 1, memories: 2 });
    expect(heat.get("f-a")).toMatchObject({ total: 11, opened: 1, memories: 3 });
  });

  it("matches whole path labels only — a.b does not capture a.bc", () => {
    const { heat } = folderHeat(folders, [mem("1", "a.bc.note", 4)]);
    expect(heat.get("f-abc")?.total).toBe(4);
    expect(heat.get("f-ab")).toBeUndefined();
  });

  it("never counts a memory toward an unrelated folder", () => {
    const { heat } = folderHeat(folders, [mem("1", "a.b.note", 4)]);
    expect(heat.get("f-z")).toBeUndefined();
  });

  it("reports the hottest folder as the scale's max", () => {
    const { max } = folderHeat(folders, [mem("1", "a.b.note", 4), mem("2", "z.note", 9)]);
    expect(max).toBe(9);
  });

  it("keeps excluded folders (the view root) out of the scale but still sums them", () => {
    const { heat, max } = folderHeat(folders, [mem("1", "a.b.note", 4)], new Set(["f-a"]));
    expect(heat.get("f-a")?.total).toBe(4); // still known — the legend reports it
    expect(max).toBe(4); // a.b sets the top, not its ancestor
    const only = folderHeat([{ id: "f-a", path: "a" }], [mem("1", "a.note", 4)], new Set(["f-a"]));
    expect(only.max).toBe(0);
  });

  it("lists a folder's top memories by total, labelled by title or path leaf", () => {
    const { heat } = folderHeat(folders, [
      mem("1", "a.b.low", 1),
      mem("2", "a.b.high", 20, 2, "High note"),
      mem("3", "a.b.mid", 5),
      mem("4", "a.b.lowest", 0, 1),
    ]);
    expect(heat.get("f-ab")?.top).toEqual([
      { label: "High note", total: 22 },
      { label: "mid", total: 5 },
      { label: "low", total: 1 },
    ]);
  });

  it("is empty with no memories", () => {
    expect(folderHeat(folders, [])).toEqual({ heat: new Map(), max: 0 });
  });
});

describe("heatRadius", () => {
  it("shrinks folders with no retrievals below every hot folder", () => {
    expect(heatRadius(0, 10)).toBe(HEAT_R_COLD);
    expect(HEAT_R_COLD).toBeLessThan(HEAT_R_MIN);
  });

  it("spans HEAT_R_MIN..HEAT_R_MAX on the shared log scale", () => {
    expect(heatRadius(100, 100)).toBe(HEAT_R_MAX);
    expect(heatRadius(25, 100)).toBeCloseTo(HEAT_R_MIN + (HEAT_R_MAX - HEAT_R_MIN) * heatFraction(25, 100));
    expect(heatRadius(1, 1_000_000)).toBeGreaterThanOrEqual(HEAT_R_MIN);
  });
});

describe("heat links", () => {
  it("tints a spoke with its hot child's color and fades spokes into cold folders", () => {
    expect(heatLinkColor({ __heat: 5, __color: "#3987e5" })).toBe("rgba(57,135,229,0.6)");
    expect(heatLinkColor({ __heat: 0, __color: "#363d4b" })).toBe(COLD_LINK);
    expect(heatLinkColor("unresolved-id")).toBe(COLD_LINK);
  });

  it("widens spokes with the child's heat", () => {
    expect(heatLinkWidth({ __heat: 100 }, 100)).toBeCloseTo(3.2);
    expect(heatLinkWidth({ __heat: 1 }, 100)).toBeGreaterThan(heatLinkWidth({ __heat: 0 }, 100));
    expect(heatLinkWidth("unresolved-id", 100)).toBe(0.3);
  });
});

describe("folderSubgraph", () => {
  const node = (id: string, type: string, parent_id: string | null = null): NodeMeta => ({
    id, path: id, parent_id, type, slug: id, title: null, tags: [], importance: 1,
  });

  it("keeps only folders and the links between two folders", () => {
    const g: GraphData = {
      nodes: [node("a", "folder"), node("b", "folder", "a"), node("n", "note", "b")],
      links: [
        { source: "a", target: "b", kind: "containment" },
        { source: "b", target: "n", kind: "containment" },
        { source: "n", target: "a", kind: "edge" },
      ],
    };
    const out = folderSubgraph(g);
    expect(out.nodes.map((n) => n.id)).toEqual(["a", "b"]);
    expect(out.links).toEqual([{ source: "a", target: "b", kind: "containment" }]);
  });
});

describe("viewRoots", () => {
  const node = (id: string): NodeMeta => ({
    id, path: id, parent_id: null, type: "folder", slug: id, title: null, tags: [], importance: 1,
  });

  it("is every node no in-view containment link points into", () => {
    const g: GraphData = {
      nodes: [node("a"), node("b"), node("c"), node("x")],
      links: [
        { source: "a", target: "b", kind: "containment" },
        { source: "b", target: "c", kind: "containment" },
        { source: "c", target: "x", kind: "edge" }, // similarity edges don't make a parent
      ],
    };
    expect([...viewRoots(g)].sort()).toEqual(["a", "x"]);
  });
});

describe("hotFolderGraph", () => {
  const node = (id: string): NodeMeta => ({
    id, path: id, parent_id: null, type: "folder", slug: id, title: null, tags: [], importance: 1,
  });
  // root ─┬─ hot ── hotter
  //       └─ cold
  const g: GraphData = {
    nodes: [node("root"), node("hot"), node("hotter"), node("cold")],
    links: [
      { source: "root", target: "hot", kind: "containment" },
      { source: "hot", target: "hotter", kind: "containment" },
      { source: "root", target: "cold", kind: "containment" },
    ],
  };
  const summed = (total: number) => ({ total, opened: 0, memories: 1, top: [] });
  const roots = new Set(["root"]);

  it("drops folders with no retrievals beneath them, keeping the root", () => {
    const heat = new Map([["root", summed(5)], ["hot", summed(5)], ["hotter", summed(5)]]);
    const out = hotFolderGraph(g, heat, roots);
    expect(out.nodes.map((n) => n.id)).toEqual(["root", "hot", "hotter"]);
    expect(out.links.map((l) => `${l.source}>${l.target}`)).toEqual(["root>hot", "hot>hotter"]);
  });

  it("shows only the root until counts arrive, so cold folders never flash in", () => {
    const out = hotFolderGraph(g, null, roots);
    expect(out.nodes.map((n) => n.id)).toEqual(["root"]);
    expect(out.links).toEqual([]);
  });
});
