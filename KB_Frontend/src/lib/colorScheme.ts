// Node color schemes for the graph view-filter overlay.
//
// A `NodeColorScheme` is the unit the filter panel switches between: a self-contained way
// of coloring memory-graph nodes to highlight one structural aspect (its type, its depth in
// the tree, its importance…). Each scheme owns BOTH halves of the story — the per-node color
// AND the legend that explains the mapping — so adding a new way to read the graph means
// adding one subclass here and listing it in COLOR_SCHEMES, with nothing else to touch.
//
// Cross-cutting concerns stay OUT of the schemes by design: the "removed from analysis"
// override and the Removed/Edge legend rows are applied by GraphView around the scheme, and
// the entity graph has its own fixed palette. Schemes only handle the normal memory node.
import type { ColorSchemeId, NodeMeta } from "../types";
import { importanceColor, recencyColor } from "./color";
import type { FolderHeatMap } from "./folderHeat";
import { HEAT_RAMP, heatStep } from "./heat";
import { pathDepth } from "./tree";

// Shared type palette — the "default" coloring, also reused by GraphView's entity fallback
// and the depth scheme (which only recolors folders, leaving notes/entities by type).
export const TYPE_COLORS: Record<string, string> = {
  folder: "#f3b65a",
  note: "#54d6a6",
  entity: "#b794f6",
};
export const FALLBACK = "#99a2b3";
// Nodes removed from entity analysis read as "off": a desaturated slate. Applied by the
// caller ABOVE any scheme, so an off node reads as off no matter how we're coloring.
export const EXCLUDED = "#5b6472";
// Folders with no agent retrievals beneath them: a dark slate that recedes into the canvas
// so the hot ones (blue heat ramp) stand out. Darker than EXCLUDED so the two never collide.
export const NOT_RETRIEVED = "#363d4b";

/** Everything a scheme needs beyond the node itself, computed once per render by GraphView. */
export type SchemeContext = {
  /** Min/max importance across the whole KB, so importance colors stay comparable. */
  importanceDomain: [number, number];
  /** Deepest folder path in the current graph, so depth colors span the visible range. */
  maxDepth: number;
  /** Min/max `updated_at` (epoch ms) across the current graph, so recency colors span it. */
  updatedDomain: [number, number];
  /** Summed all-time retrievals per in-view folder; only computed while the retrieval
   *  scheme is active (absent/null = not loaded yet). */
  retrieval?: FolderHeatMap | null;
};

/** One row in the graph legend. Rendered by GraphView from this data (no JSX in this lib). */
export type LegendEntry = {
  swatch:
    | { type: "dot"; color: string }
    | { type: "diamond"; color: string; hollow?: boolean }
    | { type: "gradient"; css: string };
  label: string;
};

/**
 * A pluggable way to color the memory graph. Subclasses capture one "color schema"; the
 * filter overlay picks the active one and the graph applies it. Kept as an abstract class
 * so each scheme is a single cohesive object — id + label for the panel,
 * `colorFor` for rendering, `legend` for the key — rather than three parallel lookup tables.
 */
export abstract class NodeColorScheme {
  /** Stable id used for persistence and selection (see {@link ColorSchemeId}). */
  abstract readonly id: ColorSchemeId;
  /** Short label shown in the filter panel. */
  abstract readonly label: string;
  /** One-line description shown under the label in the panel. */
  abstract readonly description: string;

  /**
   * Fill color for a node under this scheme. The removed/off override is applied by the
   * caller before this runs, so implementations only handle the normal case.
   */
  abstract colorFor(node: NodeMeta, ctx: SchemeContext): string;

  /**
   * Legend rows describing this scheme's color mapping. The shared Removed/Edge rows are
   * appended by the caller, so a scheme returns only the rows it is responsible for.
   */
  abstract legend(nodes: NodeMeta[], ctx: SchemeContext): LegendEntry[];
}

/** Whether any entity node is present, so a scheme can include the entity legend row. */
function hasEntity(nodes: NodeMeta[]): boolean {
  return nodes.some((n) => n.type === "entity");
}

/** The current scheme — colors every node by its kind (folder / note / entity). */
class DefaultColorScheme extends NodeColorScheme {
  readonly id = "type" as const;
  readonly label = "Default";
  readonly description = "Color by kind — folder, note, entity";

  colorFor(node: NodeMeta): string {
    return TYPE_COLORS[node.type] ?? FALLBACK;
  }

  legend(nodes: NodeMeta[]): LegendEntry[] {
    const rows: LegendEntry[] = [
      { swatch: { type: "dot", color: TYPE_COLORS.folder }, label: "Folder" },
      { swatch: { type: "dot", color: TYPE_COLORS.note }, label: "Note" },
    ];
    if (hasEntity(nodes))
      rows.push({ swatch: { type: "diamond", color: TYPE_COLORS.entity }, label: "Entity" });
    return rows;
  }
}

// Folders deepen as they nest: a shallow folder is bright gold, a deep one a dark gold —
// keeping the folder hue so the coloring still reads as "folders", just graded by depth.
// Notes/entities keep their type color so only directory structure is recolored.
const DEPTH_HUE = 38;
const DEPTH_SAT = 78;
const DEPTH_L_SHALLOW = 70;
const DEPTH_L_DEEP = 34;

/** Folder color graded by tree depth (1 = shallowest → maxDepth = deepest). */
export function depthColor(depth: number, maxDepth: number): string {
  const span = Math.max(1, maxDepth - 1);
  const t = Math.min(1, Math.max(0, (depth - 1) / span)); // 0 at the root → 1 at the deepest
  const l = Math.round(DEPTH_L_SHALLOW + t * (DEPTH_L_DEEP - DEPTH_L_SHALLOW));
  return `hsl(${DEPTH_HUE}, ${DEPTH_SAT}%, ${l}%)`;
}

/** Deepest folder path in a node set; 1 when there are no folders. */
export function maxFolderDepth(nodes: NodeMeta[]): number {
  let max = 1;
  for (const n of nodes) {
    if (n.type !== "folder") continue;
    const d = pathDepth(n.path);
    if (d > max) max = d;
  }
  return max;
}

/** Colors directory nodes by their depth in the tree; leaves notes/entities by type. */
class DepthColorScheme extends NodeColorScheme {
  readonly id = "depth" as const;
  readonly label = "By depth";
  readonly description = "Grade folders by how deep they nest";

  colorFor(node: NodeMeta, ctx: SchemeContext): string {
    if (node.type === "folder") return depthColor(pathDepth(node.path), ctx.maxDepth);
    return TYPE_COLORS[node.type] ?? FALLBACK;
  }

  legend(nodes: NodeMeta[], ctx: SchemeContext): LegendEntry[] {
    const css = `linear-gradient(90deg, ${depthColor(1, ctx.maxDepth)}, ${depthColor(
      ctx.maxDepth,
      ctx.maxDepth,
    )})`;
    const rows: LegendEntry[] = [
      { swatch: { type: "gradient", css }, label: "Shallow → Deep" },
      { swatch: { type: "dot", color: TYPE_COLORS.note }, label: "Note" },
    ];
    if (hasEntity(nodes))
      rows.push({ swatch: { type: "diamond", color: TYPE_COLORS.entity }, label: "Entity" });
    return rows;
  }
}

// The importance legend's rainbow, matching importanceColor's red→purple sweep.
const IMPORTANCE_GRADIENT =
  "linear-gradient(90deg, hsl(0,85%,60%), hsl(55,85%,60%), hsl(140,85%,55%), hsl(210,85%,60%), hsl(280,85%,62%))";

/** Colors every node by comparative importance (red = most important → purple = least). */
class ImportanceColorScheme extends NodeColorScheme {
  readonly id = "importance" as const;
  readonly label = "By importance";
  readonly description = "Rainbow from most to least important";

  colorFor(node: NodeMeta, ctx: SchemeContext): string {
    const [min, max] = ctx.importanceDomain;
    return importanceColor(node.importance ?? min, min, max);
  }

  legend(nodes: NodeMeta[]): LegendEntry[] {
    const rows: LegendEntry[] = [
      { swatch: { type: "gradient", css: IMPORTANCE_GRADIENT }, label: "High → Low importance" },
    ];
    // Entities keep their diamond shape under importance coloring; flag the shape, not a color.
    if (hasEntity(nodes))
      rows.push({ swatch: { type: "diamond", color: "rgba(231,234,241,0.7)" }, label: "Entity" });
    return rows;
  }
}

// The recency legend's heat ramp, matching recencyColor's slate→magenta→amber sweep.
const RECENCY_GRADIENT =
  "linear-gradient(90deg, hsl(220,25%,35%), hsl(263,43%,41%), hsl(305,60%,48%), hsl(348,78%,54%), hsl(30,95%,60%))";

/** Short human date for the recency legend ("Jun 12", plus the year across year spans). */
function legendDate(ms: number, withYear: boolean): string {
  return new Date(ms).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(withYear ? { year: "numeric" } : {}),
  });
}

/**
 * The timestamp the recency scheme reads: the ingest-log content change when present
 * (truthful — pipeline re-upserts don't move it), else updated_at (which they do touch).
 * Also feeds the domain computation in GraphView, so color and legend stay consistent.
 */
export function recencyTimestamp(node: NodeMeta): string | null | undefined {
  return node.content_changed_at ?? node.updated_at;
}

/** Colors every node by how recently it was modified (amber = fresh → slate = stale). */
class RecencyColorScheme extends NodeColorScheme {
  readonly id = "recency" as const;
  readonly label = "By recency";
  readonly description = "Recently modified runs hot; stale recedes";

  colorFor(node: NodeMeta, ctx: SchemeContext): string {
    const iso = recencyTimestamp(node);
    const t = iso ? Date.parse(iso) : NaN;
    if (Number.isNaN(t)) return FALLBACK;
    const [min, max] = ctx.updatedDomain;
    return recencyColor(t, min, max);
  }

  legend(nodes: NodeMeta[], ctx: SchemeContext): LegendEntry[] {
    const [min, max] = ctx.updatedDomain;
    const withYear = new Date(min).getFullYear() !== new Date(max).getFullYear();
    const rows: LegendEntry[] = [
      {
        swatch: { type: "gradient", css: RECENCY_GRADIENT },
        label: `${legendDate(min, withYear)} → ${legendDate(max, withYear)}`,
      },
    ];
    // Entities keep their diamond shape under recency coloring; flag the shape, not a color.
    if (hasEntity(nodes))
      rows.push({
        swatch: { type: "diamond", color: "rgba(231,234,241,0.7)" },
        label: "Entity",
      });
    return rows;
  }
}

const HEAT_GRADIENT = `linear-gradient(90deg, ${HEAT_RAMP.join(", ")})`;

/**
 * Colors each folder by the summed agent retrievals (surfaced + opened, all recorded history,
 * eval traffic excluded) of every memory beneath it, on the sequential blue heat ramp; folders
 * with none recede to slate. GraphView shows only folders under this scheme (see
 * lib/folderHeat.ts), so memories never reach colorFor in practice and read as cold if they do.
 */
class RetrievalColorScheme extends NodeColorScheme {
  readonly id = "retrieval" as const;
  readonly label = "By retrieval";
  readonly description = "Folders agents search most run hot";

  colorFor(node: NodeMeta, ctx: SchemeContext): string {
    const total = ctx.retrieval?.heat.get(node.id)?.total ?? 0;
    const step = heatStep(total, ctx.retrieval?.max ?? 0);
    return step < 0 ? NOT_RETRIEVED : HEAT_RAMP[step];
  }

  legend(_nodes: NodeMeta[], ctx: SchemeContext): LegendEntry[] {
    return [
      {
        swatch: { type: "gradient", css: HEAT_GRADIENT },
        label: ctx.retrieval
          ? `1 → ${ctx.retrieval.max} retrievals per folder`
          : "Loading retrievals…",
      },
      { swatch: { type: "dot", color: NOT_RETRIEVED }, label: "No retrievals" },
      {
        swatch: { type: "dot", color: FALLBACK },
        label: "Memories hidden — hover a folder for its top ones",
      },
    ];
  }
}

/** All schemes, in the order the filter panel lists them. The first is the default. */
export const COLOR_SCHEMES: readonly NodeColorScheme[] = [
  new DefaultColorScheme(),
  new DepthColorScheme(),
  new ImportanceColorScheme(),
  new RecencyColorScheme(),
  new RetrievalColorScheme(),
];

const BY_ID = new Map<ColorSchemeId, NodeColorScheme>(COLOR_SCHEMES.map((s) => [s.id, s]));

/** The scheme for an id, falling back to the default ("type") for an unknown id. */
export function colorSchemeById(id: ColorSchemeId | string | null | undefined): NodeColorScheme {
  return (id && BY_ID.get(id as ColorSchemeId)) || COLOR_SCHEMES[0];
}
