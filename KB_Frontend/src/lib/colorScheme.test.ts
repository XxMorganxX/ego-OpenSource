import { describe, expect, it } from "vitest";

import type { NodeMeta } from "../types";
import { importanceColor, recencyColor } from "./color";
import {
  colorSchemeById,
  COLOR_SCHEMES,
  depthColor,
  FALLBACK,
  maxFolderDepth,
  NOT_RETRIEVED,
  TYPE_COLORS,
} from "./colorScheme";
import { HEAT_RAMP } from "./heat";

function meta(over: Partial<NodeMeta> & { id: string; path: string; type: string }): NodeMeta {
  return { parent_id: null, slug: over.id, title: null, tags: [], importance: 1, ...over };
}

const ctx = {
  importanceDomain: [1, 4] as [number, number],
  maxDepth: 4,
  updatedDomain: [0, 1] as [number, number],
};

/** Lightness (%) out of an `hsl(h, s%, l%)` string. */
function lightness(hsl: string): number {
  const m = hsl.match(/hsl\(\s*\d+\s*,\s*\d+%\s*,\s*(\d+)%/);
  if (!m) throw new Error(`not an hsl color: ${hsl}`);
  return Number(m[1]);
}

describe("colorSchemeById", () => {
  it("looks schemes up by id", () => {
    expect(colorSchemeById("type").id).toBe("type");
    expect(colorSchemeById("depth").id).toBe("depth");
    expect(colorSchemeById("importance").id).toBe("importance");
    expect(colorSchemeById("recency").id).toBe("recency");
  });

  it("falls back to the default (first) scheme for an unknown or missing id", () => {
    expect(colorSchemeById("nope").id).toBe(COLOR_SCHEMES[0].id);
    expect(colorSchemeById(null).id).toBe(COLOR_SCHEMES[0].id);
    expect(colorSchemeById(undefined).id).toBe(COLOR_SCHEMES[0].id);
  });
});

describe("maxFolderDepth", () => {
  it("returns the deepest folder path, ignoring non-folders", () => {
    expect(
      maxFolderDepth([
        meta({ id: "1", path: "a", type: "folder" }),
        meta({ id: "2", path: "a.b.c", type: "folder" }),
        meta({ id: "3", path: "a.b.c.d.e", type: "note" }), // notes don't count
      ]),
    ).toBe(3);
  });

  it("returns 1 when there are no folders", () => {
    expect(maxFolderDepth([meta({ id: "1", path: "a.b", type: "note" })])).toBe(1);
  });
});

describe("depthColor", () => {
  it("darkens (lower lightness) as depth grows", () => {
    expect(lightness(depthColor(1, 4))).toBeGreaterThan(lightness(depthColor(4, 4)));
    expect(lightness(depthColor(2, 4))).toBeGreaterThan(lightness(depthColor(3, 4)));
  });

  it("clamps depth outside [1, maxDepth] to the endpoints", () => {
    expect(depthColor(0, 4)).toBe(depthColor(1, 4));
    expect(depthColor(9, 4)).toBe(depthColor(4, 4));
  });
});

describe("DefaultColorScheme", () => {
  const scheme = colorSchemeById("type");

  it("colors each node by its kind", () => {
    expect(scheme.colorFor(meta({ id: "1", path: "a", type: "folder" }), ctx)).toBe(
      TYPE_COLORS.folder,
    );
    expect(scheme.colorFor(meta({ id: "2", path: "b", type: "note" }), ctx)).toBe(
      TYPE_COLORS.note,
    );
  });

  it("lists Folder + Note, and Entity only when an entity is present", () => {
    const noEntity = scheme.legend([meta({ id: "1", path: "a", type: "folder" })], ctx);
    expect(noEntity.map((r) => r.label)).toEqual(["Folder", "Note"]);
    const withEntity = scheme.legend([meta({ id: "2", path: "b", type: "entity" })], ctx);
    expect(withEntity.map((r) => r.label)).toContain("Entity");
  });
});

describe("DepthColorScheme", () => {
  const scheme = colorSchemeById("depth");

  it("grades folders by depth but leaves notes/entities by type", () => {
    const shallow = scheme.colorFor(meta({ id: "1", path: "a", type: "folder" }), ctx);
    const deep = scheme.colorFor(meta({ id: "2", path: "a.b.c.d", type: "folder" }), ctx);
    expect(lightness(shallow)).toBeGreaterThan(lightness(deep));
    expect(scheme.colorFor(meta({ id: "3", path: "a.note", type: "note" }), ctx)).toBe(
      TYPE_COLORS.note,
    );
  });

  it("leads with a depth gradient", () => {
    const rows = scheme.legend([meta({ id: "1", path: "a", type: "folder" })], ctx);
    expect(rows[0].swatch.type).toBe("gradient");
  });
});

describe("RecencyColorScheme", () => {
  const scheme = colorSchemeById("recency");
  const OLD = "2026-06-12T12:00:00Z";
  const NEW = "2026-08-03T12:00:00Z";
  const rctx = {
    ...ctx,
    updatedDomain: [Date.parse(OLD), Date.parse(NEW)] as [number, number],
  };

  it("matches the shared recency ramp over the domain", () => {
    const node = meta({ id: "1", path: "a", type: "note", updated_at: NEW });
    expect(scheme.colorFor(node, rctx)).toBe(
      recencyColor(Date.parse(NEW), Date.parse(OLD), Date.parse(NEW)),
    );
  });

  it("prefers the truthful content_changed_at over updated_at", () => {
    const node = meta({
      id: "1", path: "a", type: "note",
      updated_at: NEW, content_changed_at: OLD,
    });
    expect(scheme.colorFor(node, rctx)).toBe(
      recencyColor(Date.parse(OLD), Date.parse(OLD), Date.parse(NEW)),
    );
  });

  it("falls back to updated_at when the ingest log never saw the node", () => {
    const node = meta({
      id: "1", path: "a", type: "note",
      updated_at: NEW, content_changed_at: null,
    });
    expect(scheme.colorFor(node, rctx)).toBe(
      recencyColor(Date.parse(NEW), Date.parse(OLD), Date.parse(NEW)),
    );
  });

  it("falls back to grey for a node with no timestamp", () => {
    expect(scheme.colorFor(meta({ id: "1", path: "a", type: "note" }), rctx)).toBe(FALLBACK);
  });

  it("leads with a gradient labeled with the domain dates", () => {
    const rows = scheme.legend(
      [meta({ id: "1", path: "a", type: "note", updated_at: NEW })],
      rctx,
    );
    expect(rows[0].swatch.type).toBe("gradient");
    expect(rows[0].label).toMatch(/^[A-Z][a-z]{2} \d{1,2} → [A-Z][a-z]{2} \d{1,2}$/);
  });

  it("includes the years when the domain spans multiple years", () => {
    const spanning = {
      ...ctx,
      updatedDomain: [Date.parse("2025-06-12T12:00:00Z"), Date.parse(NEW)] as [number, number],
    };
    const rows = scheme.legend(
      [meta({ id: "1", path: "a", type: "note", updated_at: NEW })],
      spanning,
    );
    expect(rows[0].label).toContain("2025");
    expect(rows[0].label).toContain("2026");
  });

  it("flags entities by shape when present", () => {
    const rows = scheme.legend(
      [meta({ id: "2", path: "b", type: "entity", updated_at: NEW })],
      rctx,
    );
    expect(rows.some((r) => r.label === "Entity" && r.swatch.type === "diamond")).toBe(true);
  });
});

describe("ImportanceColorScheme", () => {
  const scheme = colorSchemeById("importance");

  it("matches the shared importance rainbow over the domain", () => {
    const node = meta({ id: "1", path: "a", type: "note", importance: 4 });
    expect(scheme.colorFor(node, ctx)).toBe(importanceColor(4, 1, 4));
  });

  it("leads with a gradient swatch", () => {
    const rows = scheme.legend([meta({ id: "1", path: "a", type: "note" })], ctx);
    expect(rows[0].swatch.type).toBe("gradient");
  });
});

describe("RetrievalColorScheme", () => {
  const scheme = colorSchemeById("retrieval");
  const summed = (total: number) => ({ total, opened: 0, memories: 1, top: [] });
  const retrieval = {
    heat: new Map([
      ["hot", summed(100)],
      ["warm", summed(1)], // 1 of 100 → floor(5·√0.01) = step 0
    ]),
    max: 100,
  };
  const withHeat = { ...ctx, retrieval };

  it("is looked up by id", () => {
    expect(scheme.id).toBe("retrieval");
  });

  it("puts the folder with the most retrievals beneath it at the top of the heat ramp", () => {
    const node = meta({ id: "hot", path: "a", type: "folder" });
    expect(scheme.colorFor(node, withHeat)).toBe(HEAT_RAMP[HEAT_RAMP.length - 1]);
  });

  it("gives a rarely retrieved folder the bottom step", () => {
    const node = meta({ id: "warm", path: "b", type: "folder" });
    expect(scheme.colorFor(node, withHeat)).toBe(HEAT_RAMP[0]);
  });

  it("dims folders with no retrievals beneath them", () => {
    const node = meta({ id: "cold", path: "c", type: "folder" });
    expect(scheme.colorFor(node, withHeat)).toBe(NOT_RETRIEVED);
  });

  it("reads every folder as cold while counts are still loading", () => {
    const node = meta({ id: "hot", path: "a", type: "folder" });
    expect(scheme.colorFor(node, ctx)).toBe(NOT_RETRIEVED);
    expect(scheme.colorFor(node, { ...ctx, retrieval: null })).toBe(NOT_RETRIEVED);
  });

  it("labels the ramp with the hottest folder's total and explains hidden memories", () => {
    const rows = scheme.legend([meta({ id: "hot", path: "a", type: "folder" })], withHeat);
    expect(rows[0]).toEqual({
      swatch: { type: "gradient", css: `linear-gradient(90deg, ${HEAT_RAMP.join(", ")})` },
      label: "1 → 100 retrievals per folder",
    });
    expect(rows).toContainEqual({ swatch: { type: "dot", color: NOT_RETRIEVED }, label: "No retrievals" });
    expect(rows).toContainEqual({
      swatch: { type: "dot", color: FALLBACK },
      label: "Memories hidden — hover a folder for its top ones",
    });
  });

  it("says so in the legend while counts load", () => {
    const rows = scheme.legend([], ctx);
    expect(rows[0].label).toBe("Loading retrievals…");
  });
});
