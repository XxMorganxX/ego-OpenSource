// Folder-level retrieval heat for the graph's "By retrieval" view.
//
// Under that scheme the memories graph shows directories only: each folder carries the
// summed agent retrievals (surfaced + opened, all recorded history) of every memory beneath
// it, so the busy parts of the tree read at a glance instead of drowning in thousands of leaf
// dots and their containment spokes. Memories themselves surface in a folder's hover.
import type { GraphData } from "../types";
import { withAlpha } from "./color";
import { heatFraction } from "./heat";

/** One memory agents retrieved, as returned by /api/retrieval/heatmap. */
export type RetrievedMemory = {
  id: string;
  path: string;
  title: string | null;
  surfaced: number;
  opened: number;
};

/** A folder's summed retrievals and its busiest memories (for the hover). */
export type FolderHeat = {
  total: number;
  opened: number;
  memories: number;
  top: { label: string; total: number }[];
};

/** Folder heat keyed by folder node id, plus the hottest folder's total (scale top). */
export type FolderHeatMap = { heat: Map<string, FolderHeat>; max: number };

const TOP_N = 3;

/**
 * Sums each memory's retrievals into every folder above it. Matching is by whole ltree
 * label, so `a.b` never captures `a.bc`. Folders with no retrieved memory beneath them are
 * simply absent from the map. `scaleExclude` (the view root) is still summed — its total is
 * reported elsewhere — but kept out of `max`: a root holds everything beneath it, so letting
 * it set the top of the scale would flatten every real subfolder.
 */
export function folderHeat(
  folders: { id: string; path: string }[],
  memories: RetrievedMemory[],
  scaleExclude: ReadonlySet<string> = new Set(),
): FolderHeatMap {
  const idByPath = new Map(folders.map((f) => [f.path, f.id]));
  const acc = new Map<string, FolderHeat & { all: { label: string; total: number }[] }>();
  for (const m of memories) {
    const total = m.surfaced + m.opened;
    if (total <= 0) continue;
    const labels = m.path.split(".");
    const entry = { label: m.title || labels[labels.length - 1], total };
    // Every proper prefix of the memory's path is a candidate ancestor folder.
    for (let i = 1; i < labels.length; i++) {
      const id = idByPath.get(labels.slice(0, i).join("."));
      if (!id) continue;
      const h = acc.get(id) ?? { total: 0, opened: 0, memories: 0, top: [], all: [] };
      h.total += total;
      h.opened += m.opened;
      h.memories += 1;
      h.all.push(entry);
      acc.set(id, h);
    }
  }
  const heat = new Map<string, FolderHeat>();
  let max = 0;
  for (const [id, { all, ...h }] of acc) {
    h.top = all.sort((a, b) => b.total - a.total).slice(0, TOP_N);
    heat.set(id, h);
    if (h.total > max && !scaleExclude.has(id)) max = h.total;
  }
  return { heat, max };
}

// Folder radius under the retrieval view: cold folders shrink below every hot one, hot
// folders span a much wider range than the usual 4–9 px so the busiest branches dominate.
export const HEAT_R_COLD = 3;
export const HEAT_R_MIN = 5;
export const HEAT_R_MAX = 16;

/** Radius for a folder with `total` retrievals, log-scaled against the hottest (`max`). */
export function heatRadius(total: number, max: number): number {
  if (total <= 0 || max <= 0) return HEAT_R_COLD;
  return HEAT_R_MIN + (HEAT_R_MAX - HEAT_R_MIN) * heatFraction(total, max);
}

// The view root (the vault, or the folder drilled into) is the anchor, not a data point:
// neutral off-white with a ring, a fixed size, pinned at the center, labeled "· root". Its
// total is reported in the legend instead of on the node.
export const ROOT_COLOR = "#e7eaf1";
export const ROOT_R = 10;

// A spoke into a folder with no retrievals all but disappears.
export const COLD_LINK = "rgba(255,255,255,0.05)";
const HOT_LINK_ALPHA = 0.6;

/** The rendered child end of a link: d3 swaps ids for node objects once it resolves them. */
type LinkEnd = { __heat?: number; __color?: string } | string;

/** A folder→folder spoke tinted with its child's heat color; near-invisible when cold. */
export function heatLinkColor(child: LinkEnd): string {
  if (typeof child === "string" || !child.__heat || !child.__color) return COLD_LINK;
  return withAlpha(child.__color, HOT_LINK_ALPHA);
}

/** Spoke width grows with the child's heat (log scaled), so hot branches read as trunks. */
export function heatLinkWidth(child: LinkEnd, max: number): number {
  const heat = typeof child === "string" ? 0 : (child.__heat ?? 0);
  if (heat <= 0 || max <= 0) return 0.3;
  return 0.8 + 2.4 * heatFraction(heat, max);
}

/** The graph restricted to the given node ids and the links between two of them. */
function restrict(graph: GraphData, ids: ReadonlySet<string>): GraphData {
  return {
    nodes: graph.nodes.filter((n) => ids.has(n.id)),
    links: graph.links.filter((l) => ids.has(l.source) && ids.has(l.target)),
  };
}

/** The graph restricted to folders and the containment links between them. */
export function folderSubgraph(graph: GraphData): GraphData {
  return restrict(graph, new Set(graph.nodes.filter((n) => n.type === "folder").map((n) => n.id)));
}

/** The view's roots: nodes no in-view containment link points into. */
export function viewRoots(graph: GraphData): Set<string> {
  const children = new Set(graph.links.filter((l) => l.kind === "containment").map((l) => l.target));
  return new Set(graph.nodes.filter((n) => !children.has(n.id)).map((n) => n.id));
}

/**
 * The folder graph trimmed to the roots plus every folder with retrievals beneath it. A hot
 * folder's parent is always hot too (it sums the same memories), so the result stays one
 * connected tree. Before counts arrive (`heat` null) only the roots are kept, so cold folders
 * never flash in and then vanish with a re-layout.
 */
export function hotFolderGraph(
  folders: GraphData,
  heat: ReadonlyMap<string, FolderHeat> | null,
  roots: ReadonlySet<string>,
): GraphData {
  const keep = new Set(roots);
  if (heat) for (const [id, h] of heat) if (h.total > 0) keep.add(id);
  return restrict(folders, keep);
}
