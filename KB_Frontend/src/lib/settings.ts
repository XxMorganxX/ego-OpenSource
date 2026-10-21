import { COLOR_SCHEMES } from "./colorScheme";
import type { ColorSchemeId, GraphSettings } from "../types";

export const SPACING_MIN = 20;
export const SPACING_MAX = 200;
export const GRAVITY_MIN = -5;
export const GRAVITY_MAX = 150;
export const DEFAULT_GRAPH_SETTINGS: GraphSettings = {
  spacing: 50,
  gravity: 40,
  colorScheme: "type",
};

const STORAGE_KEY = "kb.graph-settings";

export function clampSpacing(n: number): number {
  if (!Number.isFinite(n)) return DEFAULT_GRAPH_SETTINGS.spacing;
  return Math.min(SPACING_MAX, Math.max(SPACING_MIN, Math.round(n)));
}

export function clampGravity(n: number): number {
  if (!Number.isFinite(n)) return DEFAULT_GRAPH_SETTINGS.gravity;
  return Math.min(GRAVITY_MAX, Math.max(GRAVITY_MIN, Math.round(n)));
}

const SCHEME_IDS = new Set<string>(COLOR_SCHEMES.map((s) => s.id));

/**
 * Resolves a stored color-scheme value to a known id, falling back to the default. Also
 * migrates the legacy `colorByImportance` boolean (its toggle moved into the view-filter
 * overlay) so old localStorage still honors a user's importance coloring on first load.
 */
export function resolveColorScheme(parsed: Partial<GraphSettings> & {
  colorByImportance?: unknown;
}): ColorSchemeId {
  if (typeof parsed.colorScheme === "string" && SCHEME_IDS.has(parsed.colorScheme))
    return parsed.colorScheme;
  if (parsed.colorByImportance) return "importance";
  return DEFAULT_GRAPH_SETTINGS.colorScheme;
}

export function loadSettings(): GraphSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_GRAPH_SETTINGS };
    const parsed = JSON.parse(raw) as Partial<GraphSettings> & { colorByImportance?: unknown };
    return {
      spacing: clampSpacing(Number(parsed.spacing)),
      gravity: clampGravity(Number(parsed.gravity)),
      colorScheme: resolveColorScheme(parsed),
    };
  } catch {
    return { ...DEFAULT_GRAPH_SETTINGS };
  }
}

export function saveSettings(settings: GraphSettings): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ ...settings, gravity: clampGravity(settings.gravity) }),
    );
  } catch {
    /* storage unavailable (private mode / quota) — settings stay in-memory */
  }
}

/**
 * Maps the dispersal knob to d3-force parameters:
 *  - `linkDistance` — rest length of containment links, i.e. how far apart
 *    connected (in-directory) nodes settle.
 *  - `chargeStrength` — many-body repulsion that spreads nodes within a cluster.
 *  - `chargeDistanceMax` — the radius past which repulsion is ignored. Capping
 *    it is essential: with d3's default of Infinity, every node repels every
 *    other across the whole graph, and that cross-cluster repulsion grows with
 *    node count — so it always overwhelms the gravity pull and disconnected
 *    subgraphs only ever fly apart. Limiting reach to a few link-lengths keeps
 *    repulsion local, letting cluster gravity actually reel the clusters in.
 */
export function forceParams(spacing: number): {
  linkDistance: number;
  chargeStrength: number;
  chargeDistanceMax: number;
} {
  return {
    linkDistance: spacing,
    chargeStrength: -spacing * 3,
    chargeDistanceMax: spacing * 3,
  };
}

/**
 * Maps the signed cluster-gravity knob (-5–150) to positional force strength.
 * Positive values pull nodes toward the active layout target, 0 disables the force,
 * and negative values reverse it. The same signed strength drives origin/focus gravity
 * and the Memories radial target force.
 */
export function gravityStrength(gravity: number): number {
  return clampGravity(gravity) / 500;
}
