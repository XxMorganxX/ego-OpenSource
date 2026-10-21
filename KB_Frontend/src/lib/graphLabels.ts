// Collision-free labels for the force graph's "By retrieval" view.
//
// Reuses the replay graph's greedy placer (lib/replayLabels.ts): labels go in priority order,
// each taking the first free spot of six around its node, else dropped. The force graph is a
// zoomable world, so placement runs in SCREEN pixels (labels are a constant on-screen size)
// and is mapped back to world coordinates for drawing. Zooming in spreads nodes apart on
// screen, so labels dropped for space come back.
import { labelBox, placeLabels, type Box } from "./replayLabels";

/** A node that wants a label, in world coordinates. Array order is priority. */
export interface GraphLabelItem {
  id: string;
  x: number;
  y: number;
  r: number;
  text: string;
}

/** A placed label, in world coordinates (`y` is the text baseline). */
export interface GraphLabel {
  id: string;
  text: string;
  x: number;
  y: number;
  anchor: "start" | "end";
  box: Box;
}

type ViewRect = { left: number; top: number; right: number; bottom: number };

// Without a captured view rect (first frame), place over everything with generous room.
const UNBOUNDED_PAD = 2000;

/**
 * Places labels for `items` (highest priority first) so no two overlap and none covers a
 * node circle in `obstacles`. `scale` is the camera zoom (world → screen px); `view` the
 * world rect on screen — labels that would fall off it are dropped.
 */
export function placeGraphLabels(
  items: GraphLabelItem[],
  obstacles: { x: number; y: number; r: number }[],
  scale: number,
  view: ViewRect | null,
): GraphLabel[] {
  const rect = view ?? unboundedRect(items, scale);
  const sx = (x: number) => (x - rect.left) * scale;
  const sy = (y: number) => (y - rect.top) * scale;
  const reserved: Box[] = obstacles.map((o) => ({
    x1: sx(o.x) - o.r * scale,
    y1: sy(o.y) - o.r * scale,
    x2: sx(o.x) + o.r * scale,
    y2: sy(o.y) + o.r * scale,
  }));
  const placed = placeLabels(
    items.map((it) => ({ id: it.id, x: sx(it.x), y: sy(it.y), r: it.r * scale, text: it.text })),
    { width: (rect.right - rect.left) * scale, height: (rect.bottom - rect.top) * scale },
    reserved,
  );
  const out: GraphLabel[] = [];
  for (const it of items) {
    const p = placed.get(it.id);
    if (!p) continue;
    const b = labelBox(p);
    out.push({
      id: p.id,
      text: p.text,
      x: p.x / scale + rect.left,
      y: p.y / scale + rect.top,
      anchor: p.anchor,
      box: {
        x1: b.x1 / scale + rect.left,
        y1: b.y1 / scale + rect.top,
        x2: b.x2 / scale + rect.left,
        y2: b.y2 / scale + rect.top,
      },
    });
  }
  return out;
}

function unboundedRect(items: GraphLabelItem[], scale: number): ViewRect {
  const pad = UNBOUNDED_PAD / scale;
  const xs = items.map((i) => i.x);
  const ys = items.map((i) => i.y);
  return {
    left: Math.min(0, ...xs) - pad,
    top: Math.min(0, ...ys) - pad,
    right: Math.max(0, ...xs) + pad,
    bottom: Math.max(0, ...ys) + pad,
  };
}
