import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";

import { fetchSubtree } from "../api/client";
import { useIsMobile } from "../hooks/useMediaQuery";
import { useRetrievalCounts } from "../hooks/useRetrievalCounts";
import { normalize, updatedDomain, withAlpha } from "../lib/color";
import {
  colorSchemeById,
  EXCLUDED,
  FALLBACK,
  type LegendEntry,
  maxFolderDepth,
  recencyTimestamp,
  type SchemeContext,
  TYPE_COLORS,
} from "../lib/colorScheme";
import { CONCEPT_KIND_COLORS } from "../lib/concepts";
import {
  folderHeat,
  folderSubgraph,
  heatLinkColor,
  heatLinkWidth,
  heatRadius,
  hotFolderGraph,
  ROOT_COLOR,
  ROOT_R,
  viewRoots,
} from "../lib/folderHeat";
import { placeGraphLabels } from "../lib/graphLabels";
import { useEntityZoom } from "../lib/entityZoom";
import {
  getSprite,
  type NodeSprite,
  SPRITE_MAX_ZOOM,
  spriteDensity,
  type SpriteKind,
  traceDiamond,
} from "../lib/nodeSprites";
import { assignRadialTargets, idJitter } from "../lib/radial";
import { forceParams, gravityStrength } from "../lib/settings";
import {
  buildConceptGraph,
  buildEntityGraph,
  buildRootGraph,
  entityMembership,
  pathDepth,
} from "../lib/tree";
import type {
  ColorSchemeId,
  Concept,
  EntityGraphData,
  EntityRole,
  GraphData,
  GraphMode,
  GraphSettings,
  NodeMeta,
} from "../types";
import { GraphControls } from "./GraphControls";
import { GraphFilterPanel } from "./GraphFilterPanel";
import { GraphIcon } from "./icons";

// Tooltip labels flow through float-tooltip's .html() (innerHTML). Paths are charset-safe
// (ltree), but titles/slugs carry ingested + LLM-coined text, so escape before render.
export function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Guard threshold before rendering a big graph. Nodes draw as pre-rendered sprites (see
// lib/nodeSprites) with viewport culling, so the canvas comfortably handles low thousands;
// past that it's the d3 layout itself that gets heavy.
export const LARGE_BRANCH_THRESHOLD = 1200;

// A minimal d3-force that nudges every node toward a center, acting like d3's
// forceX(cx)+forceY(cy). It closes the distance between disconnected subgraphs (separate
// top-level directories) without altering link rest lengths. The center is the layout
// origin by default, or — in a focused class view — the class NODE itself: the force reads
// its live position each tick, so gravity gathers the instances symmetrically around the
// class AND keeps tracking it when it's dragged (the instances follow the parent). Written
// inline so we don't take a direct d3-force dependency.
type Vec = { x: number; y: number; vx: number; vy: number };
function makeGravityForce(strength: number, center: Vec | null = null) {
  let nodes: Vec[] = [];
  const force = (alpha: number) => {
    const cx = center?.x ?? 0;
    const cy = center?.y ?? 0;
    for (const n of nodes) {
      if (n === center) continue; // the center doesn't pull on itself
      n.vx -= (n.x - cx) * strength * alpha;
      n.vy -= (n.y - cy) * strength * alpha;
    }
  };
  force.initialize = (n: Vec[]) => {
    nodes = n;
  };
  return force;
}

// A minimal d3-force collide: keeps nodes from overlapping now that folders have variable
// radii. Each node's radius is read once at initialize via `radiusFn` (so it matches what's
// drawn); on each tick, overlapping pairs are pushed apart along their connecting line by
// adjusting velocity (like d3's forceCollide), with the bigger node yielding less (split by
// radius²). A couple of relaxation passes per tick settle dense clusters. Written inline so we
// don't take a direct d3-force dependency, matching makeGravityForce.
//
// Scales near-linearly via a uniform spatial grid: nodes are binned into cells sized to the
// widest possible contact, so each node only tests the handful of neighbors in its own and the
// 8 adjacent cells rather than the whole graph. Two nodes can overlap only when their centers
// are within (rᵢ + rⱼ + pad) ≤ cellSize, which puts them in neighboring cells — so nothing is
// missed. (We adjust velocity, not position, so positions hold across passes and one grid serves
// them all.)
export function makeCollideForce(radiusFn: (n: any) => number, pad = 2, iterations = 2) {
  let nodes: any[] = [];
  let radii: number[] = [];
  let cellSize = 1; // set at initialize from the largest node + pad
  const STRENGTH = 0.7;
  const cellOf = (v: number) => Math.floor(v / cellSize);
  const force = () => {
    const n = nodes.length;
    if (n === 0) return;
    const grid = new Map<string, number[]>();
    for (let i = 0; i < n; i++) {
      const key = cellOf(nodes[i].x) + "," + cellOf(nodes[i].y);
      const bucket = grid.get(key);
      if (bucket) bucket.push(i);
      else grid.set(key, [i]);
    }
    for (let pass = 0; pass < iterations; pass++) {
      for (let i = 0; i < n; i++) {
        const a = nodes[i];
        const cx = cellOf(a.x);
        const cy = cellOf(a.y);
        for (let gx = cx - 1; gx <= cx + 1; gx++) {
          for (let gy = cy - 1; gy <= cy + 1; gy++) {
            const bucket = grid.get(gx + "," + gy);
            if (!bucket) continue;
            for (const j of bucket) {
              if (j <= i) continue; // visit each pair once
              const b = nodes[j];
              let dx = b.x - a.x;
              let dy = b.y - a.y;
              const rmin = radii[i] + radii[j] + pad;
              let d2 = dx * dx + dy * dy;
              if (d2 >= rmin * rmin) continue;
              if (d2 === 0) {
                // Coincident: nudge along a deterministic axis so they don't stay stuck.
                dx = 1;
                dy = 0;
                d2 = 1;
              }
              const d = Math.sqrt(d2);
              const push = ((rmin - d) / d) * STRENGTH;
              const sx = dx * push;
              const sy = dy * push;
              const ri2 = radii[i] * radii[i];
              const rj2 = radii[j] * radii[j];
              const wa = rj2 / (ri2 + rj2); // smaller node absorbs more of the displacement
              const wb = ri2 / (ri2 + rj2);
              a.vx -= sx * wa;
              a.vy -= sy * wa;
              b.vx += sx * wb;
              b.vy += sy * wb;
            }
          }
        }
      }
    }
  };
  force.initialize = (n: any[]) => {
    nodes = n;
    radii = nodes.map((node) => radiusFn(node));
    // Cell = widest possible contact distance, so any overlapping pair lands in adjacent cells.
    const maxR = radii.reduce((m, r) => Math.max(m, r), 0);
    cellSize = Math.max(1, 2 * maxR + pad);
  };
  return force;
}

// A minimal d3-force for the radial memories layout: pulls each node toward its stamped
// depth-ring radius (__targetR) and wedge-midpoint angle (__targetTheta) — targets come
// from lib/radial.ts and are restamped in the attribute pass. This is the memories-mode
// replacement for origin gravity (the gravity knob scales it): the radial pull forms the
// depth rings, and a weaker tangential pull keeps each cluster inside its own wedge so
// disconnected subtrees can't drift into each other. Velocity-adjusting, written inline
// so we don't take a direct d3-force dependency, matching its sibling forces above.
export const RADIAL_TANGENTIAL = 0.4; // angular pull as a fraction of the radial strength
export function makeRadialForce(strength: number) {
  let nodes: any[] = [];
  const TWO_PI = Math.PI * 2;
  const force = (alpha: number) => {
    if (strength === 0) return;
    for (const n of nodes) {
      const tr = n.__targetR;
      if (tr === undefined) continue; // not a memories node — nothing stamped
      const d = Math.hypot(n.x, n.y);
      if (tr === 0) {
        // The drilled-in root belongs at the center: a plain origin pull.
        n.vx -= n.x * strength * alpha;
        n.vy -= n.y * strength * alpha;
        continue;
      }
      const theta = n.__targetTheta ?? 0;
      // Radial unit vector; a node sitting on the origin exits along its wedge midpoint.
      const onOrigin = d < 1e-6;
      const ux = onOrigin ? Math.cos(theta) : n.x / d;
      const uy = onOrigin ? Math.sin(theta) : n.y / d;
      const push = (tr - d) * strength * alpha;
      n.vx += ux * push;
      n.vy += uy * push;
      if (!onOrigin) {
        // Tangential arc-length correction toward the wedge midpoint, shortest way
        // around the circle (theta is in [0, 2π); atan2 returns (-π, π]).
        let dt = theta - Math.atan2(n.y, n.x);
        dt = ((dt % TWO_PI) + TWO_PI) % TWO_PI;
        if (dt > Math.PI) dt -= TWO_PI;
        const arc = dt * d * strength * RADIAL_TANGENTIAL * alpha;
        n.vx += -uy * arc;
        n.vy += ux * arc;
      }
    }
  };
  force.initialize = (n: any[]) => {
    nodes = n;
  };
  return force;
}

// A minimal d3-force that makes each memories tree branch OUT from the root: every folder
// repels the nodes DEEPER than itself (higher stamped __targetDepth), and the push grows
// linearly with the folder's own nesting depth — so the rings near the center stay calm
// while deeply nested directories actively shove descendants (their own and any stray
// neighbors') outward and apart. Only the deeper node moves (the folder holds its ring, the
// radial force owns its position), which biases every branch to unfold away from the center
// instead of folding back across shallower rings. Same 1/distance falloff and hard range
// cap as d3's forceManyBody; grid-binned like makeCollideForce so it stays near-linear.
// Written inline so we don't take a direct d3-force dependency, matching its siblings.
export const DEPTH_REPULSION = 0.6; // per-depth-level push as a fraction of charge strength
export function makeDepthRepulsionForce(strength: number, distanceMax: number) {
  let nodes: any[] = [];
  const cellSize = Math.max(1, distanceMax);
  const cellOf = (v: number) => Math.floor(v / cellSize);
  const force = (alpha: number) => {
    if (strength === 0 || nodes.length === 0) return;
    const grid = new Map<string, any[]>();
    for (const n of nodes) {
      if (n.__targetDepth === undefined) continue; // not a memories node — nothing stamped
      const key = cellOf(n.x) + "," + cellOf(n.y);
      const bucket = grid.get(key);
      if (bucket) bucket.push(n);
      else grid.set(key, [n]);
    }
    const max2 = distanceMax * distanceMax;
    for (const f of nodes) {
      const depth = f.__targetDepth;
      if (!depth || f.type !== "folder") continue; // only nested directories repel
      const cx = cellOf(f.x);
      const cy = cellOf(f.y);
      for (let gx = cx - 1; gx <= cx + 1; gx++) {
        for (let gy = cy - 1; gy <= cy + 1; gy++) {
          const bucket = grid.get(gx + "," + gy);
          if (!bucket) continue;
          for (const n of bucket) {
            if (n.__targetDepth <= depth) continue; // only strictly deeper nodes are pushed
            const dx = n.x - f.x;
            const dy = n.y - f.y;
            const d2 = dx * dx + dy * dy;
            // Out of range, or coincident (the collide force separates those).
            if (d2 >= max2 || d2 === 0) continue;
            // forceManyBody's shape: push ∝ 1/distance, clamped near contact so a node
            // sitting almost on the folder can't be launched.
            const w = (strength * depth * alpha) / Math.max(d2, 1);
            n.vx += dx * w;
            n.vy += dy * w;
          }
        }
      }
    }
  };
  force.initialize = (n: any[]) => {
    nodes = n;
  };
  return force;
}

// Entity-graph palette: classes are the bright, saturated purple; instances a lighter
// lavender (and drawn hollow, see nodeCanvasObject) so they read as secondary; the
// `entities` hub is a neutral grey so it reads as scaffolding, not a concept.
const ENTITY_COLORS: Record<EntityRole, string> = {
  root: "#99a2b3",
  class: "#b794f6",
  instance: "#cdbdf2",
};

/** Concept hub radius: grows gently with member count, capped so big hubs stay sane. */
export function conceptRadius(memberCount: number): number {
  return Math.min(13, 5.5 + Math.sqrt(Math.max(0, memberCount)) * 0.9);
}

const EDGE = "#6ea8ff";

// Direct-path node drawing — the pre-sprite look, kept as the fallback for camera zooms
// past SPRITE_MAX_ZOOM (vector paths stay crisp at any magnification, and culling means
// only a handful of nodes are on screen there) and for environments that can't rasterize
// sprites (jsdom). Must render the same look the sprites bake in.
function drawNodeVector(
  ctx: CanvasRenderingContext2D,
  n: { x: number; y: number },
  kind: SpriteKind,
  color: string,
  r: number,
  scale: number,
) {
  ctx.save();
  ctx.beginPath();
  if (kind === "circle") ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
  else traceDiamond(ctx, n.x, n.y, r);
  if (kind === "diamond-hollow") {
    // A concrete instance reads as secondary to its class: a small HOLLOW diamond —
    // faint fill, crisp outline, no glow — against the class's solid, glowing diamond.
    ctx.globalAlpha = 0.16;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.lineWidth = 1.6 / scale;
    ctx.strokeStyle = color;
    ctx.stroke();
  } else {
    ctx.shadowColor = color;
    ctx.shadowBlur = 14;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.lineWidth = 1.4 / scale;
    ctx.strokeStyle = "rgba(255,255,255,0.35)";
    ctx.stroke();
  }
  ctx.restore();
}

// Off-analysis = self OR an ancestor folder excluded (so a removed folder's whole subtree
// dims, not just the folder). Falls back to the own-flag if effective isn't supplied.
const isOff = (n: any): boolean =>
  n.analysis_excluded_effective ?? n.analysis_excluded ?? false;

// Folder node sizing. A folder's radius blends two structural signals so the graph's shape
// reads at a glance: how high it sits in the tree, and how significant it is. Notes/entities
// keep fixed radii; only folders scale.
export const FOLDER_R_MIN = 4;
export const FOLDER_R_MAX = 9;
const FOLDER_DEPTH_WEIGHT = 0.5; // the rest (0.5) is significance — an even mix

/**
 * Radius for a folder node, mixing depth and significance onto [FOLDER_R_MIN, FOLDER_R_MAX].
 * Both inputs are ABSOLUTE (whole-KB), not relative to what's on screen, so a folder is the same
 * size at the root or drilled into a subtree — folders stay sized relative to one another on one
 * stable scale.
 *  - depth: the folder's true depth in the tree (1 = top-level). Shallower → bigger (1/√depth decay).
 *  - significance: the node's importance normalized to [0, 1] across the whole-KB importance scale.
 */
export function folderRadius(depth: number, significance: number): number {
  const depthFactor = 1 / Math.sqrt(Math.max(1, depth)); // 1, .71, .58, .5…
  const sig = Math.min(1, Math.max(0, significance));
  const mix = FOLDER_DEPTH_WEIGHT * depthFactor + (1 - FOLDER_DEPTH_WEIGHT) * sig;
  return FOLDER_R_MIN + (FOLDER_R_MAX - FOLDER_R_MIN) * mix;
}
const CONTAINMENT = "rgba(255,255,255,0.16)";
// Retrieval view: how many of the hottest folders are labeled regardless of zoom.
const HOT_LABELS = 10;
// Retrieval view: ring/link spacing multiplier over the spacing knob.
const RETRIEVAL_SPACING = 2.5;

type Props = {
  mode: GraphMode;
  onModeChange: (mode: GraphMode) => void;
  folderPath: string | null;
  nodes: NodeMeta[];
  /** Full entity graph (hub + classes + instances); null while it loads. */
  entityData: EntityGraphData | null;
  /** Focused class id in entities mode (null = show all classes). */
  entityClass: string | null;
  /** Active concepts (raw payload); null while loading. Concepts mode only. */
  concepts: Concept[] | null;
  conceptsError: string | null;
  onRetryConcepts: () => void;
  /** Opens the read-only concept card for a clicked concept hub. */
  onSelectConcept: (id: string) => void;
  showInstances: boolean;
  onShowInstancesChange: (value: boolean) => void;
  /** Reveal inactive (never-activated) entity classes; hidden by default. */
  showInactive: boolean;
  onShowInactiveChange: (value: boolean) => void;
  /** Focuses an entity class (drill into its instances); null clears the focus. */
  onSelectClass: (id: string | null) => void;
  settings: GraphSettings;
  /** Persists a settings change made from the in-graph view-filter overlay. */
  onSettingsChange: (settings: GraphSettings) => void;
  importanceDomain: [number, number];
  /** Bumped by the parent on each live DB change; refetches the open subtree. */
  liveVersion?: number;
  onSelectNode: (id: string) => void;
};

export function shouldShowNodeLabel(
  node: { role?: string },
  scale: number,
  isConcepts: boolean,
  isMobile = false,
) {
  // Phones reserve labels for grouping-level nodes (concepts, entity classes and
  // their root): individual node names stay hidden at any zoom so the small
  // canvas stays readable — tapping a node opens its detail instead.
  if (isMobile)
    return node.role === "concept" || node.role === "class" || node.role === "root";
  return (isConcepts && node.role === "concept") || scale > 1.3;
}

/** The entity graph's shared-space frame: the class nodes and the hub. */
export function isFrameNode(node: { role?: string }) {
  return node.role === "class" || node.role === "root";
}

export function isTopLevelConceptNode(node: { role?: string; parent_id?: string | null }) {
  return node.role === "concept" && node.parent_id === null;
}

export function GraphView({
  mode,
  onModeChange,
  folderPath,
  nodes,
  entityData,
  entityClass,
  concepts,
  conceptsError,
  onRetryConcepts,
  onSelectConcept,
  showInstances,
  onShowInstancesChange,
  showInactive,
  onShowInactiveChange,
  onSelectClass,
  settings,
  onSettingsChange,
  importanceDomain,
  liveVersion = 0,
  onSelectNode,
}: Props) {
  const fgRef = useRef<any>(null);
  // Mobile trims node labels to grouping-level titles (see shouldShowNodeLabel).
  const isMobile = useIsMobile();
  // The graph pane. ForceGraph2D defaults its canvas to the full window when no size is
  // given, so it would spill over the detail panel (covering its close button). We measure
  // this pane and pass explicit dimensions so the canvas always fits — and reflows when the
  // panel opens/closes or the window resizes.
  const containerRef = useRef<HTMLDivElement>(null);
  const [dims, setDims] = useState({ width: 0, height: 0 });
  // ForceGraph2D stores each node's simulation state (x/y/vx/vy/fx/fy) on the node
  // object itself, and only keeps the layout for objects whose identity is stable across
  // renders. We cache nodes by id so a live refetch reuses the same objects — refreshing
  // their data fields in place — instead of handing the graph brand-new, position-less
  // nodes that would re-layout the whole structure from scratch on every DB change.
  const nodeCacheRef = useRef<Map<string, any>>(new Map());
  const [subtree, setSubtree] = useState<GraphData | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  // The edge link under the pointer. Directional particles run ONLY on this link: an
  // always-on particle animation defeats force-graph's autoPauseRedraw (live photons force
  // a repaint every frame), keeping the canvas hot at 60fps even when nothing is moving.
  // Static direction comes from mid-link arrowheads instead; hovering an edge animates it.
  const [hoverLink, setHoverLink] = useState<object | null>(null);
  // World-space rect currently in view, captured once per frame in onRenderFramePre.
  // Node painters cull against it so panning/zooming a large graph only pays for what's
  // on screen. Null = unknown → draw everything.
  const viewRectRef = useRef<{
    left: number;
    right: number;
    top: number;
    bottom: number;
  } | null>(null);
  const folderRef = useRef(folderPath);
  folderRef.current = folderPath;
  const modeRef = useRef(mode);
  modeRef.current = mode;

  // Track the pane's size so the canvas matches it. Measured synchronously on mount (no
  // first-paint flash) and kept in sync via a ResizeObserver — which fires when the detail
  // panel slides in and the graph flex-shrinks.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => setDims({ width: el.clientWidth, height: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    setConfirmed(false);
    setSubtree(null);
    if (!folderPath) return;
    let live = true;
    fetchSubtree(folderPath).then((g) => {
      if (live) setSubtree(g);
    });
    return () => {
      live = false;
    };
  }, [folderPath]);

  // Live refresh: on a DB change, refetch the open subtree in place (no remount, so the
  // graph doesn't flash to a spinner). Keyed on liveVersion only; the current folder is
  // read from a ref so this never double-fetches on a folder switch. Entities mode refetches
  // its graph in the parent, so we skip the subtree refetch there.
  useEffect(() => {
    if (liveVersion === 0 || modeRef.current !== "memories") return;
    const path = folderRef.current;
    if (!path) return;
    let live = true;
    fetchSubtree(path).then((g) => {
      if (live) setSubtree(g);
    });
    return () => {
      live = false;
    };
  }, [liveVersion]);

  // Root view: top-level folders become disconnected clusters and root files
  // become standalone nodes. Selecting a folder swaps in its fetched subtree.
  const rootGraph = useMemo(() => buildRootGraph(nodes), [nodes]);
  const entityGraph = useMemo(
    () =>
      mode === "entities" && entityData
        ? buildEntityGraph(entityData, { focusClassId: entityClass, showInstances, showInactive })
        : null,
    [mode, entityData, entityClass, showInstances, showInactive],
  );
  const conceptGraph = useMemo(
    () => (mode === "concepts" && concepts ? buildConceptGraph(concepts) : null),
    [mode, concepts],
  );
  // The active color scheme (picked in the view-filter overlay) drives memory-node coloring
  // and its legend; entities mode keeps its own fixed role palette.
  const scheme = colorSchemeById(settings.colorScheme);
  // "By retrieval" turns the memories graph into a directory heat view: only folders with
  // retrievals beneath them are drawn (memory dots, cold folders and their spokes drown the
  // signal), each carrying the summed agent retrievals of everything beneath it. Filtering
  // before layout lets the folders settle on their own. The view root (the vault, or the
  // folder drilled into) is the anchor: centered, neutral, and out of the heat scale.
  const retrievalView = mode === "memories" && scheme.id === "retrieval";
  // Hot folders are up to 16 px and labeled, so the retrieval view spreads its rings (and
  // link/charge forces) wider than the dot-sized default or the first ring piles up on the root.
  const layoutSpacing = retrievalView ? settings.spacing * RETRIEVAL_SPACING : settings.spacing;
  const retrievedMemories = useRetrievalCounts(retrievalView);
  const memoriesGraph = folderPath ? subtree : rootGraph;
  const folderGraph = useMemo(
    () => (retrievalView && memoriesGraph ? folderSubgraph(memoriesGraph) : null),
    [retrievalView, memoriesGraph],
  );
  const heatRoots = useMemo(
    () => (folderGraph ? viewRoots(folderGraph) : new Set<string>()),
    [folderGraph],
  );
  const folderHeatMap = useMemo(
    () =>
      folderGraph && retrievedMemories
        ? folderHeat(
            folderGraph.nodes.filter((n) => n.type === "folder"),
            retrievedMemories,
            heatRoots,
          )
        : null,
    [folderGraph, retrievedMemories, heatRoots],
  );
  const shownMemoriesGraph = useMemo(
    () =>
      folderGraph
        ? hotFolderGraph(folderGraph, folderHeatMap?.heat ?? null, heatRoots)
        : memoriesGraph,
    [folderGraph, folderHeatMap, heatRoots, memoriesGraph],
  );
  const data: GraphData | null =
    mode === "entities" ? entityGraph : mode === "concepts" ? conceptGraph : shownMemoriesGraph;
  // The hottest subfolders are labeled at every zoom so the busy branches name themselves
  // (the root is always labeled on its own terms).
  const hotLabelIds = useMemo(() => {
    if (!folderHeatMap) return new Set<string>();
    const ranked = [...folderHeatMap.heat.entries()]
      .filter(([id]) => !heatRoots.has(id))
      .sort((a, b) => b[1].total - a[1].total);
    return new Set(ranked.slice(0, HOT_LABELS).map(([id]) => id));
  }, [folderHeatMap, heatRoots]);
  // The root's total lives in the legend header, not on the node.
  const retrievalScope = useMemo(() => {
    if (!retrievalView || !folderHeatMap || !data || heatRoots.size !== 1) return null;
    const [rootId] = heatRoots;
    const root = data.nodes.find((n) => n.id === rootId);
    if (!root) return null;
    const h = folderHeatMap.heat.get(rootId);
    if (!h) return `Root: ${root.path} · no retrievals`;
    const count = h.memories === 1 ? "1 memory" : `${h.memories} memories`;
    return `Root: ${root.path} · ${h.total} retrievals across ${count}`;
  }, [retrievalView, folderHeatMap, data, heatRoots]);
  // Radial layout targets (memories mode only): depth rings + subtree-proportional wedges
  // from the in-view containment links — see lib/radial.ts.
  // Computed from the RAW payload (before the cached-node build below) so a brand-new
  // node can be seeded at its target instead of d3's default placement. Ring gap = the
  // spacing knob's link distance, so a parent→child link spans exactly one ring.
  const isMemories = mode === "memories";
  const radialTargets = useMemo(
    () =>
      isMemories && data
        ? assignRadialTargets(data.nodes, data.links, forceParams(layoutSpacing).linkDistance)
        : null,
    [isMemories, data, layoutSpacing],
  );
  // instance -> its class ids, from the membership links (category is no longer parent_id)
  const instanceClasses = useMemo(
    () => (entityData ? entityMembership(entityData).instanceClasses : new Map<string, Set<string>>()),
    [entityData],
  );
  const belongsToFocus = (n: any): boolean =>
    n.role === "instance" && !!entityClass && (instanceClasses.get(n.id)?.has(entityClass) ?? false);

  const graphDataRef = useRef<GraphData>({ nodes: [], links: [] });
  const graphSigRef = useRef<string>("");
  const graphData = useMemo(() => {
    const cache = nodeCacheRef.current;
    if (!data) {
      cache.clear();
      graphSigRef.current = "";
      graphDataRef.current = { nodes: [], links: [] };
      return graphDataRef.current;
    }
    const present = new Set<string>();
    const nodes = data.nodes.map((n) => {
      present.add(n.id);
      const existing = cache.get(n.id);
      // Refresh the data fields onto the cached object but keep d3's x/y/vx/vy/fx/fy,
      // so a node that survives the refetch holds its position instead of resetting.
      if (existing) return Object.assign(existing, n);
      const created: any = { ...n };
      // First appearance: seed at the radial target (plus a tiny deterministic offset) so
      // the tree unfolds already untangled — crossings in force layouts come mostly from
      // bad starts. Cached nodes above keep their settled positions across refetches.
      const t = radialTargets?.get(n.id);
      if (t) {
        const j = idJitter(n.id);
        created.x = Math.cos(t.theta) * t.r + j.dx;
        created.y = Math.sin(t.theta) * t.r + j.dy;
      }
      cache.set(n.id, created);
      return created;
    });
    // Drop nodes that disappeared so the cache can't grow unbounded and a removed-then-
    // re-added node doesn't snap back to a stale position.
    for (const id of cache.keys()) if (!present.has(id)) cache.delete(id);
    // Keep graphData referentially STABLE across a live data-only refresh: the field
    // updates above already landed on the cached node objects in place, so when the node +
    // link set is unchanged we hand back the very same graphData object. react-force-graph
    // treats an unchanged reference as "no change" and leaves the settled layout untouched,
    // instead of re-running (and visibly drifting) the simulation on every feed tick.
    const sig =
      data.nodes.map((n) => n.id).join(",") +
      "::" +
      data.links.map((l) => `${l.source}>${l.target}`).join(",");
    if (sig === graphSigRef.current) return graphDataRef.current;
    graphSigRef.current = sig;
    // Links are always fresh clones: d3 mutates link.source/target from ids into node
    // refs in place, so reusing them across builds would corrupt the resolution.
    graphDataRef.current = { nodes, links: data.links.map((l) => ({ ...l })) };
    return graphDataRef.current;
  }, [data, radialTargets]);

  const isEntities = mode === "entities";
  const isConcepts = mode === "concepts";
  const [impMin, impMax] = importanceDomain;

  // Stamp each node's render-constant attributes (__color / __r) up front instead of
  // recomputing them inside the per-frame canvas callbacks — scheme colors build hsl()
  // strings and folder radii split paths, which is pure allocation churn at 60fps. Restamps
  // when the node set changes (graphData), a live refetch refreshes fields in place (data),
  // or any coloring input changes; __sprite resets so nodes re-resolve their bitmap.
  useMemo(() => {
    const schemeCtx: SchemeContext = {
      importanceDomain,
      maxDepth: maxFolderDepth(graphData.nodes),
      updatedDomain: updatedDomain(graphData.nodes.map(recencyTimestamp)),
      retrieval: folderHeatMap,
    };
    for (const n of graphData.nodes as any[]) {
      // Summed retrievals beneath a folder (retrieval view only) — drives size, label, links.
      n.__heat = retrievalView ? (folderHeatMap?.heat.get(n.id)?.total ?? 0) : undefined;
      // The retrieval view's root is the anchor, not a data point (see ROOT_COLOR).
      n.__root = retrievalView && heatRoots.has(n.id);
      // A lone root sits pinned at the exact center; the pin is released on leaving the view
      // (the node object is cached and reused by the other views).
      if (n.__root && heatRoots.size === 1) {
        n.fx = 0;
        n.fy = 0;
        n.__pinned = true;
      } else if (n.__pinned) {
        n.fx = undefined;
        n.fy = undefined;
        n.__pinned = false;
      }
      // Removed-from-analysis wins over every scheme — an off node reads as off no matter
      // how we're coloring. (Entities can't be excluded.)
      if (retrievalView) {
        n.__color = isOff(n)
          ? EXCLUDED
          : n.__root
            ? ROOT_COLOR
            : scheme.colorFor(n, schemeCtx);
        n.__r = n.__root ? ROOT_R : heatRadius(n.__heat, folderHeatMap?.max ?? 0);
      } else {
        n.__color = isOff(n)
          ? EXCLUDED
          : isEntities
            ? (ENTITY_COLORS[n.role as EntityRole] ?? TYPE_COLORS.entity)
            : isConcepts
              ? n.role === "concept"
                ? (CONCEPT_KIND_COLORS[n.conceptKind ?? "other"] ??
                  CONCEPT_KIND_COLORS.other)
                : n.memberKind === "entity"
                  ? ENTITY_COLORS.instance
                  : TYPE_COLORS.note
              : scheme.colorFor(n, schemeCtx);
        n.__r = isEntities
          ? n.role === "class"
            ? 7
            : n.role === "instance"
              ? 3.8
              : 5 // the hub
          : isConcepts
            ? n.role === "concept"
              ? conceptRadius(n.memberCount ?? 0)
              : n.memberKind === "entity"
                ? 3.2
                : 4
            : n.type === "folder"
              ? // Absolute depth + whole-KB significance: same size in every view.
                folderRadius(
                  pathDepth(n.path),
                  normalize(n.importance ?? impMin, impMin, impMax),
                )
              : n.type === "entity"
                ? 5.2
                : 4.5;
      }
      n.__sprite = undefined;
      // Radial targets for the memories layout; undefined in other modes (and for any
      // node the geometry couldn't place), which makes the radial force skip the node.
      const t = radialTargets?.get(n.id);
      n.__targetR = t?.r;
      n.__targetTheta = t?.theta;
      n.__targetDepth = t?.depth;
    }
  }, [
    graphData,
    data,
    isEntities,
    isConcepts,
    scheme,
    retrievalView,
    folderHeatMap,
    heatRoots,
    importanceDomain,
    impMin,
    impMax,
    radialTargets,
  ]);

  // Push the spacing knob into the live d3-force simulation. Reruns whenever the
  // graph remounts (new data / confirmed guard) so the forces stick. `graphData` is now
  // referentially stable across data-only refreshes, so this only reheats on a real
  // structural change (or a spacing/gravity tweak), not on every live feed tick.
  const centerForceRef = useRef<unknown>(undefined);
  // Radii are stamped onto nodes as __r by the attribute pass below; the collide force
  // reads them through this accessor so collision sizes always track what's drawn.
  const radiusFnRef = useRef<(n: any) => number>((n: any) => n?.__r ?? 4.5);
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    const { linkDistance, chargeStrength, chargeDistanceMax } = forceParams(layoutSpacing);
    fg.d3Force("charge")?.strength(chargeStrength).distanceMax(chargeDistanceMax);
    fg.d3Force("link")?.distance(linkDistance);
    // Memories only: nested directories shove strictly deeper nodes outward and apart —
    // scaled by the directory's own depth — so branches unfold from the root instead of
    // folding back over the inner rings (see makeDepthRepulsionForce).
    fg.d3Force(
      "depthRepel",
      mode === "memories"
        ? makeDepthRepulsionForce(-chargeStrength * DEPTH_REPULSION, chargeDistanceMax)
        : null,
    );
    // A focused class view is a single connected cluster (the frozen class + its instances)
    // anchored off-origin, so the layout's two origin-seeking forces would shift the FREE
    // instances toward the origin (the frozen scaffolding can't move) — a sideways pull that
    // stops them sitting evenly around the class. We keep gravity active but re-aim it:
    const focusedView = mode === "entities" && !!entityClass;
    //  - Gravity: still on (the strength setting stays meaningful), but centered on the
    //    focused class NODE so it gathers the instances symmetrically rather than toward
    //    origin — and, since it tracks the node's live position, the instances keep
    //    following the class when it's dragged.
    const focusNode = focusedView
      ? (nodeCacheRef.current.get(entityClass as string) as Vec | undefined)
      : undefined;
    // Memories mode swaps origin gravity for the radial ring/wedge pull — the gravity
    // knob now scales ring crispness there (0 = free-floating, exactly as before).
    fg.d3Force(
      "gravity",
      mode === "memories"
        ? makeRadialForce(gravityStrength(settings.gravity))
        : makeGravityForce(gravityStrength(settings.gravity), focusNode ?? null),
    );
    //  - force-graph's built-in forceCenter (re-centres the whole graph's centroid on the
    //    origin every tick) is origin-only, so it stays off while focused and is restored
    //    after. Captured once on first run.
    if (centerForceRef.current === undefined)
      centerForceRef.current = fg.d3Force("center") ?? null;
    fg.d3Force("center", focusedView ? null : centerForceRef.current);
    // Stop variable-size folders (and clustered entity instances) from overlapping. Sized from
    // the live radiusOf via the ref, so collision radii track what's actually drawn.
    fg.d3Force("collide", makeCollideForce((n) => radiusFnRef.current(n)));
    fg.d3ReheatSimulation();
  }, [layoutSpacing, settings.gravity, graphData, confirmed, mode, entityClass]);

  // Experimental shared-space zoom. The all-entities graph and a drilled-in class live in
  // ONE space: classes and the hub are always present and keep their positions; drilling
  // in just hides the rest and reveals the focused class' instances in place. `nodeVisible`
  // is the single source of truth for what's currently shown — used for rendering and for
  // framing the camera (so a fit ignores the hidden-but-present nodes).
  const nodeVisible = (n: any): boolean =>
    !isEntities || !entityClass || n.id === entityClass || belongsToFocus(n);
  // Hand the predicate to the zoom hook via a ref so its zoomToFit always frames the live
  // visible set without re-subscribing on every focus change. The count rides along for
  // the hook's sparse-view check — it can't read the data off the force-graph ref itself
  // (react-force-graph doesn't expose graphData() there).
  const visibleRef = useRef(nodeVisible);
  visibleRef.current = nodeVisible;
  const visibleCountRef = useRef(0);
  // Only entities mode consults the count (at focus-fit arm time), so skip the O(n)
  // filter on every render of the other modes.
  visibleCountRef.current = isEntities
    ? graphData.nodes.filter(nodeVisible).length
    : 0;
  const {
    fading,
    zoomIntoClass,
    handleEngineStop,
    handleZoomEnd,
    handleZoom,
    zoomInteractionAllowed,
    exitProgressRef,
  } = useEntityZoom(
    fgRef,
    isEntities ? entityClass : null,
    isEntities,
    onSelectClass,
    visibleRef,
    visibleCountRef,
  );
  // Exit-gesture crossfade (see useEntityZoom): as a focused class is scrolled out of,
  // `exitProgressRef` ramps 0→1 with the gesture. The hidden frame (other classes + hub)
  // ghosts IN at that alpha, the focused instances fade OUT at its complement, and the
  // focused class node anchors both views at full opacity — so by the time the exit
  // triggers, the overview is already on screen and the data swap is invisible.
  // force-graph re-reads these accessors every animation frame, so the mix tracks the
  // wheel live without any React re-renders.
  const exitT = () => (isEntities && entityClass ? exitProgressRef.current : 0);
  const nodeAlpha = (n: any): number => {
    const t = exitT();
    if (t === 0) return 1;
    if (!nodeVisible(n)) return t; // ghosted frame node fading in
    if (n.role === "instance") return 1 - t; // focused instances fading out
    return 1; // the focused class itself — the continuity anchor
  };
  const nodeVisibleOrGhost = (n: any): boolean =>
    nodeVisible(n) || (exitT() > 0 && isFrameNode(n));
  // On each focus transition, keep the shared space stable: freeze every class + the hub
  // where they sit (so nothing drifts behind the hidden view and zoom-out finds them
  // exactly where they were), and drop the focused class' freshly-added instances in a
  // ring around ITS position — so they appear where the class already is, not from origin.
  const prevClassRef = useRef<string | null>(null);
  useEffect(() => {
    if (!isEntities) {
      // Leaving entities mode with a focus active: the frame is still frozen (fx/fy set
      // below), and a pin that survives into another mode holds the node wherever it sat —
      // the radial layout could never pull it onto its ring. Release exactly what the
      // freeze pinned. (Surviving nodes keep their entities role through Object.assign,
      // so isFrameNode still identifies them here.)
      if (prevClassRef.current !== null) {
        for (const n of nodeCacheRef.current.values()) {
          if (isFrameNode(n)) {
            n.fx = undefined;
            n.fy = undefined;
          }
        }
      }
      prevClassRef.current = null;
      return;
    }
    const cache = nodeCacheRef.current;
    const prev = prevClassRef.current;
    prevClassRef.current = entityClass;
    // Same focus (e.g. a live refetch) — leave the settled layout untouched.
    if (prev === entityClass) return;
    const all = [...cache.values()];

    if (entityClass) {
      for (const n of all) {
        if (isFrameNode(n)) {
          n.fx = n.x;
          n.fy = n.y;
        }
      }
      const focus = cache.get(entityClass);
      if (focus) {
        const insts = all.filter((n) => belongsToFocus(n));
        // Seed at roughly the force layout's equilibrium radius (measured ~60 + 1.2·n),
        // so instances appear already spread instead of fanning out from a too-wide ring
        // and visibly contracting. Capped so very large classes still start sane.
        const r = Math.min(170, 52 + insts.length * 1.5);
        insts.forEach((n, i) => {
          const a = (i / Math.max(insts.length, 1)) * Math.PI * 2;
          n.x = (focus.x ?? 0) + Math.cos(a) * r;
          n.y = (focus.y ?? 0) + Math.sin(a) * r;
        });
      }
    } else {
      // Release the freeze: the classes stay exactly where they are (same space) — they're
      // just movable again now that the all-entities graph is back.
      for (const n of all) {
        if (isFrameNode(n)) {
          n.fx = undefined;
          n.fy = undefined;
        }
      }
    }
  }, [isEntities, entityClass, graphData]);
  // Count what's actually displayed (the showInactive filter applies), not the payload.
  const classCount = entityGraph
    ? entityGraph.nodes.filter((n) => n.role === "class").length
    : 0;
  const focusedClass =
    entityClass && entityData
      ? entityData.nodes.find((n) => n.id === entityClass)
      : null;

  const controls = (
    <GraphControls
      mode={mode}
      entityClass={entityClass}
      showInstances={showInstances}
      onModeChange={onModeChange}
      onShowInstancesChange={onShowInstancesChange}
      showInactive={showInactive}
      onShowInactiveChange={onShowInactiveChange}
    />
  );

  let body: React.ReactNode;
  if (isConcepts && conceptsError) {
    body = (
      <div className="guard">
        <div className="guard-card">
          <span className="guard-glyph">
            <GraphIcon />
          </span>
          <div className="guard-title">Concepts could not be loaded</div>
          <div className="guard-text">The concept graph is unavailable right now.</div>
          <button className="btn primary" onClick={onRetryConcepts}>
            Retry
          </button>
        </div>
      </div>
    );
  } else if (!data) {
    body = (
      <div className="guard">
        <div className="spinner" />
        <div className="guard-text">
          {isEntities ? "Loading entities…" : isConcepts ? "Loading concepts…" : "Loading subtree…"}
        </div>
      </div>
    );
  } else if (isEntities && data.nodes.length === 0) {
    body = (
      <div className="guard">
        <div className="guard-card">
          <span className="guard-glyph">
            <GraphIcon />
          </span>
          <div className="guard-title">No entities yet</div>
          <div className="guard-text">
            Entities (people, places, concepts) appear here as they're discovered.
          </div>
        </div>
      </div>
    );
  } else if (isConcepts && data.nodes.length === 0) {
    body = (
      <div className="guard">
        <div className="guard-card">
          <span className="guard-glyph">
            <GraphIcon />
          </span>
          <div className="guard-title">No concepts yet</div>
          <div className="guard-text">
            Run <code>kb-consolidate --apply</code> to discover the projects and topics
            your memories cluster into.
          </div>
        </div>
      </div>
    );
  } else if (!isEntities && !folderPath && data.nodes.length === 0) {
    body = (
      <div className="guard">
        <div className="guard-card">
          <span className="guard-glyph">
            <GraphIcon />
          </span>
          <div className="guard-title">Nothing to graph yet</div>
          <div className="guard-text">
            Add notes and folders to see the knowledge base laid out here.
          </div>
        </div>
      </div>
    );
  } else if (data.nodes.length > LARGE_BRANCH_THRESHOLD && !confirmed) {
    const isRoot = !isEntities && !folderPath;
    body = (
      <div className="guard">
        <div className="guard-card">
          <span className="guard-glyph">
            <GraphIcon />
          </span>
          <div className="guard-title">{isRoot ? "Large graph" : "Large branch"}</div>
          <div className="guard-text">
            {isRoot ? "The root graph has" : "This subtree has"}{" "}
            <b>{data.nodes.length}</b> nodes. Rendering may be heavy.
          </div>
          <button className="btn primary" onClick={() => setConfirmed(true)}>
            Render anyway
          </button>
        </div>
      </div>
    );
  } else {
    const hasEdges = data.links.some((l) => l.kind === "edge");
    const hasExcluded = data.nodes.some((n) => isOff(n));
    const conceptCount = isConcepts
      ? data.nodes.filter((node: any) => node.role === "concept").length
      : 0;
    const conceptMemberCount = isConcepts
      ? data.nodes.filter((node: any) => node.role === "member").length
      : 0;
    // Node colors/radii were stamped as __color/__r by the attribute pass above; the legend
    // still needs the scheme context (`maxDepth` lets the depth scheme span the visible
    // range, `updatedDomain` gives the recency legend its date endpoints).
    const schemeCtx: SchemeContext = {
      importanceDomain,
      maxDepth: maxFolderDepth(data.nodes),
      updatedDomain: updatedDomain(data.nodes.map(recencyTimestamp)),
      retrieval: folderHeatMap,
    };

    body = (
      <>
        <ForceGraph2D
          ref={fgRef}
          width={dims.width}
          height={dims.height}
          graphData={graphData}
          backgroundColor="rgba(0,0,0,0)"
          nodeLabel={(n: any) => {
            const name = escapeHtml(n.path || n.title || n.slug || "");
            if (!retrievalView) return name;
            const h = folderHeatMap?.heat.get(n.id);
            if (!h) return `${name} · no retrievals`;
            const top = h.top.map((t) => `${escapeHtml(t.label)} (${t.total})`).join(", ");
            const count = h.memories === 1 ? "1 memory" : `${h.memories} memories`;
            return `${name} · ${h.total} retrievals across ${count} — top: ${top}`;
          }}
          nodeRelSize={5}
          nodeVisibility={(n: any) => nodeVisibleOrGhost(n)}
          // Ghost LINKS stay hover-detectable (unlike ghost nodes there's no per-link
          // pointer-paint hook to suppress them) — safe today because the ghosted set is
          // hub↔class containment spokes and every hover affordance (hoverLink state,
          // particles) is gated on kind === "edge".
          linkVisibility={(l: any) =>
            nodeVisibleOrGhost(typeof l.source === "object" ? l.source : { id: l.source }) &&
            nodeVisibleOrGhost(typeof l.target === "object" ? l.target : { id: l.target })
          }
          onRenderFramePost={(ctx: CanvasRenderingContext2D, scale: number) => {
            if (retrievalView)
              drawRetrievalLabels(ctx, scale, graphData.nodes as any[], {
                hot: hotLabelIds,
                view: viewRectRef.current,
                isMobile,
              });
          }}
          onRenderFramePre={(ctx: CanvasRenderingContext2D) => {
            // Capture the visible world rect once per frame so the node painters can cull.
            try {
              const inv = ctx.getTransform().inverse();
              const a = inv.transformPoint(new DOMPoint(0, 0));
              const b = inv.transformPoint(
                new DOMPoint(ctx.canvas.width, ctx.canvas.height),
              );
              viewRectRef.current = {
                left: Math.min(a.x, b.x),
                right: Math.max(a.x, b.x),
                top: Math.min(a.y, b.y),
                bottom: Math.max(a.y, b.y),
              };
            } catch {
              viewRectRef.current = null; // no DOMMatrix support — draw everything
            }
          }}
          nodeCanvasObject={(n: any, ctx, scale) => {
            const r = n.__r ?? 4.5;
            const showLabel =
              shouldShowNodeLabel(n, scale, isConcepts, isMobile) ||
              (retrievalView && (n.__root || hotLabelIds.has(n.id)));
            // Cull nodes wholly outside the viewport. The pad covers the glow halo and,
            // when labels are on, a generous label half-width — so panning while zoomed
            // in only pays for the nodes actually on screen.
            const view = viewRectRef.current;
            if (view) {
              const pad = r * 2.5 + 6 + (showLabel ? 160 / scale : 0);
              if (
                n.x < view.left - pad ||
                n.x > view.right + pad ||
                n.y < view.top - pad ||
                n.y > view.bottom + pad
              )
                return;
            }
            // Exit crossfade: ghosts draw at the gesture's progress, focused instances at
            // its complement. globalAlpha covers the sprite, vector path and label alike.
            const alpha = nodeAlpha(n);
            if (alpha <= 0) return;
            if (alpha < 1) {
              ctx.save();
              ctx.globalAlpha = alpha;
            }
            const color = n.__color ?? FALLBACK;
            // Entities are a different kind of thing (people, places, concepts) than
            // folders/notes, so they get a diamond instead of a circle — a shape cue
            // that reads even under the importance color mode. Instances draw hollow,
            // secondary to their class's solid glowing diamond.
            const kind: SpriteKind =
              isEntities && n.role === "instance"
                ? "diamond-hollow"
                : isConcepts && n.memberKind === "entity"
                  ? "diamond-hollow"
                  : n.type === "entity"
                    ? "diamond"
                    : "circle";
            // Sprite path: one drawImage of a pre-rendered bitmap (glow baked in) whose
            // resolution tracks the camera's zoom bucket — higher-res sprites resolve as
            // you dive in, so nodes stay crisp. Past the top bucket, or if rasterization
            // is unavailable, draw the vector path directly (crisp at any magnification,
            // and by then culling leaves only a handful of nodes on screen).
            let sprite: NodeSprite | null = null;
            if (scale <= SPRITE_MAX_ZOOM) {
              const density = spriteDensity(scale);
              if (n.__sprite === undefined || n.__spriteDensity !== density) {
                n.__sprite = getSprite(kind, color, r, density);
                n.__spriteDensity = density;
              }
              sprite = n.__sprite;
            }
            if (sprite) {
              const half = sprite.worldSize / 2;
              ctx.drawImage(
                sprite.canvas,
                n.x - half,
                n.y - half,
                sprite.worldSize,
                sprite.worldSize,
              );
            } else {
              drawNodeVector(ctx, n, kind, color, r, scale);
            }
            // Outer ring: top-level concept hubs, and the retrieval view's root anchor.
            if ((isConcepts && isTopLevelConceptNode(n)) || n.__root) {
              ctx.save();
              ctx.beginPath();
              ctx.arc(n.x, n.y, r + 3 / scale, 0, 2 * Math.PI);
              ctx.globalAlpha = 0.65;
              ctx.lineWidth = 1.5 / scale;
              ctx.strokeStyle = color;
              ctx.stroke();
              ctx.restore();
            }
            // The retrieval view places its labels collision-free in onRenderFramePost.
            if (showLabel && !retrievalView) {
              const label = n.slug ?? n.path;
              const bold = isConcepts && n.role === "concept";
              ctx.font = `${bold ? 600 : 400} ${11 / scale}px Inter, sans-serif`;
              ctx.textAlign = "center";
              ctx.textBaseline = "top";
              const y = n.y + r + 4 / scale;
              const width = ctx.measureText(label).width;
              ctx.fillStyle = "rgba(11,14,20,0.78)";
              ctx.fillRect(
                n.x - width / 2 - 3 / scale,
                y - 1 / scale,
                width + 6 / scale,
                13 / scale,
              );
              ctx.fillStyle = "rgba(231,234,241,0.9)";
              ctx.fillText(label, n.x, y);
            }
            if (alpha < 1) ctx.restore();
          }}
          nodePointerAreaPaint={(n: any, color, ctx) => {
            // Ghosted frame nodes (exit crossfade) are scenery, not targets — no
            // hover label, no click, until the view actually swaps.
            if (!nodeVisible(n)) return;
            // Track the drawn radius (variable for folders) so a big folder stays
            // clickable across its whole face, with a small floor for tiny nodes.
            const r = Math.max(7, (n.__r ?? 4.5) + 2);
            const view = viewRectRef.current;
            if (
              view &&
              (n.x < view.left - r ||
                n.x > view.right + r ||
                n.y < view.top - r ||
                n.y > view.bottom + r)
            )
              return;
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
            ctx.fill();
          }}
          linkColor={(l: any) => {
            // Retrieval view: a folder→folder spoke takes its child's heat, so hot branches
            // read as bright trunks and cold ones all but disappear.
            const base = retrievalView
              ? heatLinkColor(l.target)
              : l.kind === "edge"
                ? EDGE
                : CONTAINMENT;
            if (exitT() === 0) return base;
            // Exit crossfade: a link tracks its endpoints — ghost spokes fade in with
            // the frame, instance links fade out with their nodes.
            const a = Math.min(
              nodeAlpha(typeof l.source === "object" ? l.source : { id: l.source }),
              nodeAlpha(typeof l.target === "object" ? l.target : { id: l.target }),
            );
            return a >= 1 ? base : withAlpha(base, a);
          }}
          linkWidth={(l: any) =>
            retrievalView
              ? heatLinkWidth(l.target, folderHeatMap?.max ?? 0)
              : l.kind === "edge"
                ? 1.6
                : l.weight
                  ? 0.6 + l.weight * 1.2
                  : 0.8
          }
          linkDirectionalArrowLength={(l: any) => (l.kind === "edge" ? 3 : 0)}
          linkDirectionalParticles={(l: any) =>
            l === hoverLink && l.kind === "edge" ? 2 : 0
          }
          linkDirectionalParticleWidth={2}
          linkDirectionalParticleColor={() => EDGE}
          onLinkHover={(l: any) => setHoverLink(l && l.kind === "edge" ? l : null)}
          enableZoomInteraction={zoomInteractionAllowed}
          onEngineStop={handleEngineStop}
          onZoom={handleZoom}
          onZoomEnd={handleZoomEnd}
          onNodeClick={(n: any) => {
            if (isConcepts && n.role === "concept") {
              onSelectConcept(n.id);
              return;
            }
            // Every node opens its detail panel (entity memories live there). A class
            // node in the all-entities view ALSO drills in — one gesture does both.
            onSelectNode(n.id);
            if (isEntities && !entityClass && n.role === "class") zoomIntoClass(n);
          }}
        />
        <GraphLegend
          isEntities={isEntities}
          isConcepts={isConcepts}
          conceptKinds={[
            ...new Set(
              data.nodes
                .filter((n: any) => n.role === "concept")
                .map((n: any) => n.conceptKind ?? "other"),
            ),
          ]}
          focusedClass={focusedClass}
          classCount={classCount}
          conceptCount={conceptCount}
          conceptMemberCount={conceptMemberCount}
          schemeLegend={scheme.legend(data.nodes, schemeCtx)}
          folderPath={folderPath}
          scope={retrievalScope}
          nodeCount={data.nodes.length}
          hasInstances={data.nodes.some((n) => (n as any).role === "instance")}
          hasExcluded={hasExcluded}
          hasEdges={hasEdges}
        />
      </>
    );
  }

  return (
    <div
      ref={containerRef}
      className={`graph${isEntities ? " entities" : ""}${fading ? " fading" : ""}`}
    >
      {controls}
      {mode === "memories" && (
        <GraphFilterPanel
          scheme={settings.colorScheme}
          onSchemeChange={(id: ColorSchemeId) =>
            onSettingsChange({ ...settings, colorScheme: id })
          }
        />
      )}
      {body}
    </div>
  );
}

/**
 * The retrieval view's labels, placed collision-free in one pass per frame (see
 * lib/graphLabels.ts). Priority: the root, then folders by summed retrievals — so when space
 * is tight the busiest folders keep their names and quieter ones drop (they come back as you
 * zoom in, and every folder still names itself on hover). Labeled: the root, the hottest
 * folders at any zoom, and everything else once zoomed in (the usual zoom rule).
 */
function drawRetrievalLabels(
  ctx: CanvasRenderingContext2D,
  scale: number,
  nodes: any[],
  opts: {
    hot: ReadonlySet<string>;
    view: { left: number; top: number; right: number; bottom: number } | null;
    isMobile: boolean;
  },
) {
  const { hot, view, isMobile } = opts;
  const inView = (n: any) =>
    Number.isFinite(n.x) &&
    Number.isFinite(n.y) &&
    (!view || (n.x >= view.left && n.x <= view.right && n.y >= view.top && n.y <= view.bottom));
  const visible = nodes.filter(inView);
  const wanted = visible
    .filter((n) => n.__root || hot.has(n.id) || shouldShowNodeLabel(n, scale, false, isMobile))
    .sort((a, b) => Number(!!b.__root) - Number(!!a.__root) || (b.__heat ?? 0) - (a.__heat ?? 0));
  const labels = placeGraphLabels(
    wanted.map((n) => {
      const name = n.slug ?? n.path;
      // The root says what it is; its total lives in the legend header.
      const text = n.__root ? `${name} · root` : n.__heat > 0 ? `${name} · ${n.__heat}` : name;
      return { id: n.id, x: n.x, y: n.y, r: n.__r ?? 4.5, text };
    }),
    // Labels stay off the nodes they name. Small unlabeled dots may sit under a label's
    // background — reserving every dot left no room around the dense hot clusters, so
    // busy folders lost their names.
    wanted.map((n) => ({ x: n.x, y: n.y, r: n.__r ?? 4.5 })),
    scale,
    view,
  );
  const roots = new Set(wanted.filter((n) => n.__root).map((n) => n.id));
  ctx.save();
  ctx.textBaseline = "alphabetic";
  for (const l of labels) {
    ctx.font = `${roots.has(l.id) ? 600 : 400} ${11 / scale}px Inter, sans-serif`;
    ctx.textAlign = l.anchor === "start" ? "left" : "right";
    ctx.fillStyle = "rgba(11,14,20,0.78)";
    const pad = 2 / scale;
    ctx.fillRect(l.box.x1 - pad, l.box.y1 - pad, l.box.x2 - l.box.x1 + 2 * pad, l.box.y2 - l.box.y1 + 2 * pad);
    ctx.fillStyle = "rgba(231,234,241,0.9)";
    ctx.fillText(l.text, l.x, l.y);
  }
  ctx.restore();
}

/** Renders one legend swatch from a scheme's {@link LegendEntry} (dot / diamond / gradient). */
function LegendSwatch({ swatch }: { swatch: LegendEntry["swatch"] }) {
  if (swatch.type === "gradient")
    return <span className="legend-gradient" style={{ background: swatch.css }} />;
  if (swatch.type === "diamond")
    return (
      <span
        className="legend-diamond"
        style={
          swatch.hollow
            ? { background: "transparent", border: `1.5px solid ${swatch.color}` }
            : { background: swatch.color }
        }
      />
    );
  return <span className="legend-dot" style={{ background: swatch.color }} />;
}

/** The bottom-left legend. Entities mode shows its fixed role key; memories mode mirrors the
 *  active color scheme (its rows come straight from `scheme.legend`), then the shared
 *  Removed/Edge rows. Purely presentational — everything it shows is derived data. */
function GraphLegend({
  isEntities,
  isConcepts,
  conceptKinds,
  focusedClass,
  classCount,
  conceptCount,
  conceptMemberCount,
  schemeLegend,
  folderPath,
  scope,
  nodeCount,
  hasInstances,
  hasExcluded,
  hasEdges,
}: {
  isEntities: boolean;
  isConcepts: boolean;
  conceptKinds: string[];
  focusedClass: { slug: string } | null | undefined;
  classCount: number;
  conceptCount: number;
  conceptMemberCount: number;
  schemeLegend: LegendEntry[];
  folderPath: string | null;
  /** Memories-mode header override (the retrieval view's "Root: … · N retrievals" line). */
  scope: string | null;
  nodeCount: number;
  hasInstances: boolean;
  hasExcluded: boolean;
  hasEdges: boolean;
}) {
  return (
    <div className="graph-legend">
      {isConcepts ? (
        <>
          <span className="legend-scope">
            Concepts · {conceptCount} concepts · {conceptMemberCount} members
          </span>
          {conceptKinds.map((k) => (
            <span className="legend-item" key={k}>
              <span
                className="legend-dot"
                style={{ background: CONCEPT_KIND_COLORS[k] ?? CONCEPT_KIND_COLORS.other }}
              />
              {k}
            </span>
          ))}
          <span className="legend-item">
            <span
              className="legend-diamond"
              style={{
                background: "transparent",
                border: `1.5px solid ${ENTITY_COLORS.instance}`,
              }}
            />
            Entity member
          </span>
        </>
      ) : isEntities ? (
        <>
          <span className="legend-scope">
            {focusedClass
              ? `Entities · ${focusedClass.slug}`
              : `Entities · ${classCount} ${classCount === 1 ? "class" : "classes"}`}
          </span>
          <span className="legend-item">
            <span className="legend-diamond" style={{ background: ENTITY_COLORS.class }} />
            Class
          </span>
          {hasInstances && (
            <span className="legend-item">
              <span
                className="legend-diamond"
                style={{
                  background: "transparent",
                  border: `1.5px solid ${ENTITY_COLORS.instance}`,
                }}
              />
              Instance
            </span>
          )}
        </>
      ) : (
        <>
          {scope ? (
            <span className="legend-scope">{scope}</span>
          ) : (
            !folderPath && <span className="legend-scope">Root · {nodeCount} nodes</span>
          )}
          {schemeLegend.map((entry) => (
            <span className="legend-item" key={entry.label}>
              <LegendSwatch swatch={entry.swatch} />
              {entry.label}
            </span>
          ))}
          {hasExcluded && (
            <span className="legend-item">
              <span className="legend-dot" style={{ background: EXCLUDED }} />
              Removed
            </span>
          )}
          {hasEdges && (
            <span className="legend-item">
              <span className="legend-line" />
              Edge
            </span>
          )}
        </>
      )}
    </div>
  );
}
