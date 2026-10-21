import type { ReplayRecord } from "../types";

/**
 * Deterministic layout for one search's replay graph.
 * Radius = similarity to the query (closer = more similar; none = outer ring).
 * Angle = how the memory was found: keyword-only left, meaning-only right, both top,
 * crawl-only bottom (clustered round its first parent's hub). Same input, same
 * coordinates — re-watching a replay never reshuffles it.
 */
export const VIEW_W = 1000;
export const VIEW_H = 700;
export const CENTER = { x: 420, y: 330 };
export const R_MIN = 70;
export const R_MAX = 290;
export const R_RING = 320;
const HUB_R = 170;
const CHILD_R = 46;

export type Sector = "kw" | "meaning" | "both" | "crawl" | "unknown";

export interface LayoutNode {
  id: string;
  kind: "query" | "memory" | "hub";
  x: number;
  y: number;
  sector: Sector | null;
  sim: number | null;
}

export interface LayoutEdge {
  id: string;
  from: string;
  to: string;
  kind: "fts" | "knn" | "entry" | "parent";
}

export interface Layout {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
}

// Degrees, maths orientation (0 = right, 90 = up); y is flipped on output.
const SECTORS: Record<Sector, [number, number]> = {
  both: [60, 120],
  kw: [130, 230],
  meaning: [-50, 50],
  // The free gap between the keyword (ends 230) and meaning (starts 310) sectors -- hubs
  // must not land on the meaning arc, where the top-ranked rows cluster.
  crawl: [232, 308],
  unknown: [0, 360],
};

export const hubId = (id: string) => `hub:${id}`;

export function radiusFor(sim: number | null): number {
  if (sim === null) return R_RING;
  const s = Math.min(1, Math.max(0, sim));
  return R_MIN + (1 - s) * (R_MAX - R_MIN);
}

/** FNV-1a of the id → a stable angular offset in [-3°, 3°] that separates coincident nodes. */
export function jitterDeg(id: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return ((h >>> 0) % 601) / 100 - 3;
}

function polar(deg: number, r: number, cx = CENTER.x, cy = CENTER.y) {
  const a = (deg * Math.PI) / 180;
  return { x: cx + r * Math.cos(a), y: cy - r * Math.sin(a) };
}

/** Similarity per memory id: kNN cosines first, then fused/final rows that carry one. */
export function similarityMap(rec: ReplayRecord): Map<string, number> {
  const m = new Map<string, number>();
  const take = (rows: { id: string; sim?: number | null }[] | undefined) => {
    for (const r of rows ?? []) if (r.sim != null && !m.has(r.id)) m.set(r.id, r.sim);
  };
  take(rec.flat.knn);
  take(rec.flat.fused);
  take(rec.crawl?.final);
  return m;
}

export function buildLayout(rec: ReplayRecord): Layout {
  const sims = similarityMap(rec);
  const fts = new Set((rec.flat.fts ?? []).map((r) => r.id));
  const knn = new Set((rec.flat.knn ?? []).map((r) => r.id));
  const crawlParent = new Map<string, string>();
  for (const hop of rec.crawl?.hops ?? [])
    for (const o of hop.offered ?? [])
      if (!crawlParent.has(o.id) && o.parents.length) crawlParent.set(o.id, o.parents[0].id);

  // Every memory id once, in first-seen order: final/fused order, then arms, then crawl.
  const order: string[] = [];
  const seen = new Set<string>();
  const add = (id: string) => {
    if (!seen.has(id)) {
      seen.add(id);
      order.push(id);
    }
  };
  (rec.crawl?.final ?? rec.flat.fused).forEach((r) => add(r.id));
  rec.flat.fused.forEach((r) => add(r.id));
  (rec.flat.fts ?? []).forEach((r) => add(r.id));
  (rec.flat.knn ?? []).forEach((r) => add(r.id));
  for (const hop of rec.crawl?.hops ?? []) (hop.offered ?? []).forEach((o) => add(o.id));

  const sectorOf = (id: string): Sector =>
    fts.has(id) && knn.has(id) ? "both"
      : fts.has(id) ? "kw"
        : knn.has(id) ? "meaning"
          : crawlParent.has(id) ? "crawl"
            : "unknown";

  const nodes: LayoutNode[] = [{ id: "query", kind: "query", ...CENTER, sector: null, sim: null }];
  const edges: LayoutEdge[] = [];

  const entry = rec.crawl?.entry ?? [];
  const hubPos = new Map<string, { x: number; y: number }>();
  const [h0, h1] = SECTORS.crawl;
  // Hubs: entry seeds first, then any other parent a hop offered (hop-2 bridge entities).
  const hubIds = entry.map((e) => e.id);
  for (const hop of rec.crawl?.hops ?? [])
    for (const o of hop.offered ?? [])
      for (const par of o.parents) if (!hubIds.includes(par.id)) hubIds.push(par.id);
  const entryIds = new Set(entry.map((e) => e.id));
  hubIds.forEach((id, i) => {
    const p = polar(h0 + ((i + 0.5) / hubIds.length) * (h1 - h0), HUB_R);
    hubPos.set(id, p);
    nodes.push({ id: hubId(id), kind: "hub", ...p, sector: "crawl", sim: null });
    if (entryIds.has(id)) edges.push({ id: `entry:${id}`, from: "query", to: hubId(id), kind: "entry" });
  });

  const bySector = new Map<Sector, string[]>();
  for (const id of order) {
    const s = sectorOf(id);
    bySector.set(s, [...(bySector.get(s) ?? []), id]);
  }

  const children = new Map<string, number>();
  for (const [sector, ids] of bySector) {
    const [a0, a1] = SECTORS[sector];
    ids.forEach((id, i) => {
      const parent = crawlParent.get(id);
      const hub = parent !== undefined ? hubPos.get(parent) : undefined;
      let p: { x: number; y: number };
      if (sector === "crawl" && hub) {
        const k = children.get(parent!) ?? 0;
        children.set(parent!, k + 1);
        p = polar(-90 + k * 37 + jitterDeg(id), CHILD_R + 14 * Math.floor(k / 10), hub.x, hub.y);
      } else {
        p = polar(a0 + ((i + 0.5) / ids.length) * (a1 - a0) + jitterDeg(id),
                  radiusFor(sims.get(id) ?? null));
      }
      nodes.push({ id, kind: "memory", ...p, sector, sim: sims.get(id) ?? null });
      if (fts.has(id)) edges.push({ id: `fts:${id}`, from: "query", to: id, kind: "fts" });
      if (knn.has(id)) edges.push({ id: `knn:${id}`, from: "query", to: id, kind: "knn" });
    });
  }

  for (const hop of rec.crawl?.hops ?? [])
    for (const o of hop.offered ?? [])
      for (const par of o.parents)
        edges.push({ id: `parent:${par.id}:${o.id}`, from: hubId(par.id), to: o.id, kind: "parent" });

  return { nodes, edges };
}
