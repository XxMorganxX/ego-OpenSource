/**
 * Collision-free label placement for the replay graph.
 *
 * Labels are placed greedily in priority order (the caller's array order): each tries a fixed
 * ring of spots around its node — right, left, then above/below on either side — and takes the
 * first whose box stays inside the view and clear of every label already placed (and any
 * reserved box). A label with no free spot is dropped: the row is still identified by its rank
 * in the results strip and by the hover tooltip, which is better than an unreadable pile.
 *
 * Box sizes are estimated from character count (no DOM measurement), so placement is pure,
 * deterministic and testable in jsdom.
 */
export const MAX_CHARS = 32;
/** Average glyph advance of the 12px label font — slightly generous so estimates over-cover. */
const CHAR_W = 6.8;
const ASCENT = 10;
const DESCENT = 3;
const GAP = 4;

export interface LabelRequest {
  id: string;
  /** Node centre and radius, in view coordinates. */
  x: number;
  y: number;
  r: number;
  text: string;
}

export interface PlacedLabel {
  id: string;
  text: string;
  /** Text anchor point (SVG `x`/`y`, baseline). */
  x: number;
  y: number;
  anchor: "start" | "end";
}

export interface Box {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export function truncate(text: string, max = MAX_CHARS): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

export function labelBox(p: PlacedLabel): Box {
  const w = p.text.length * CHAR_W;
  const x1 = p.anchor === "start" ? p.x : p.x - w;
  return { x1, y1: p.y - ASCENT, x2: x1 + w, y2: p.y + DESCENT };
}

const overlaps = (a: Box, b: Box) => a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;

function candidates({ x, y, r }: LabelRequest): Array<Pick<PlacedLabel, "x" | "y" | "anchor">> {
  return [
    { x: x + r + GAP, y: y + 4, anchor: "start" },          // right
    { x: x - r - GAP, y: y + 4, anchor: "end" },            // left
    { x: x + r, y: y - r - DESCENT, anchor: "start" },      // above-right
    { x: x + r, y: y + r + ASCENT + 2, anchor: "start" },   // below-right
    { x: x - r, y: y - r - DESCENT, anchor: "end" },        // above-left
    { x: x - r, y: y + r + ASCENT + 2, anchor: "end" },     // below-left
  ];
}

/** Place `requests` (highest priority first); returns id → placement for the labels that fit. */
export function placeLabels(requests: LabelRequest[], bounds: { width: number; height: number },
                            reserved: Box[] = []): Map<string, PlacedLabel> {
  const taken: Box[] = [...reserved];
  const placed = new Map<string, PlacedLabel>();
  for (const req of requests) {
    const text = truncate(req.text);
    for (const c of candidates(req)) {
      const p: PlacedLabel = { id: req.id, text, ...c };
      const b = labelBox(p);
      if (b.x1 < 0 || b.y1 < 0 || b.x2 > bounds.width || b.y2 > bounds.height) continue;
      if (taken.some((t) => overlaps(b, t))) continue;
      taken.push(b);
      placed.set(req.id, p);
      break;
    }
  }
  return placed;
}
