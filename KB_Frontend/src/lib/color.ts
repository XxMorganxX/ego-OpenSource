// Importance-based rainbow: most important nodes are red, least are purple.
// Hue 0° (red) → 280° (purple) spans the visible spectrum in rainbow order.
const HUE_RED = 0;
const HUE_PURPLE = 280;

/** Min/max importance across a set of values; [0, 1] when empty. */
export function importanceDomain(values: number[]): [number, number] {
  if (values.length === 0) return [0, 1];
  let min = Infinity;
  let max = -Infinity;
  for (const v of values) {
    if (v < min) min = v;
    if (v > max) max = v;
  }
  return [min, max];
}

/** Position of `value` within [min, max], clamped to [0, 1]; 0.5 if degenerate. */
export function normalize(value: number, min: number, max: number): number {
  if (!(max > min)) return 0.5;
  return Math.min(1, Math.max(0, (value - min) / (max - min)));
}

/**
 * Rainbow color for a node's importance, comparative across [min, max].
 * value === max → red (most important); value === min → purple (least).
 */
export function importanceColor(value: number, min: number, max: number): string {
  const t = normalize(value, min, max); // 1 at the most important
  const hue = HUE_RED + (1 - t) * (HUE_PURPLE - HUE_RED);
  return `hsl(${Math.round(hue)}, 85%, 60%)`;
}

// Recency heat: oldest nodes are dim slate-blue, newest hot amber. Hue travels
// 220° → 390° (through magenta/red, never green) so the ramp keeps the classic
// cool→hot reading; saturation and lightness rise with freshness so stale nodes
// recede into the dark background.
const RECENCY_OLD = { h: 220, s: 25, l: 35 };
const RECENCY_NEW = { h: 390, s: 95, l: 60 }; // 390 ≡ 30 (amber) once wrapped

/** Min/max epoch ms across a set of ISO timestamps; [0, 1] when none parse. */
export function updatedDomain(values: Array<string | null | undefined>): [number, number] {
  let min = Infinity;
  let max = -Infinity;
  for (const v of values) {
    if (!v) continue;
    const t = Date.parse(v);
    if (Number.isNaN(t)) continue;
    if (t < min) min = t;
    if (t > max) max = t;
  }
  if (min === Infinity) return [0, 1];
  return [min, max];
}

/**
 * Heat color for a node's last-modified time, comparative across [min, max].
 * value === max → hot amber (just touched); value === min → dim slate-blue (stale).
 */
export function recencyColor(value: number, min: number, max: number): string {
  const t = normalize(value, min, max); // 1 at the freshest
  const h = Math.round(RECENCY_OLD.h + t * (RECENCY_NEW.h - RECENCY_OLD.h)) % 360;
  const s = Math.round(RECENCY_OLD.s + t * (RECENCY_NEW.s - RECENCY_OLD.s));
  const l = Math.round(RECENCY_OLD.l + t * (RECENCY_NEW.l - RECENCY_OLD.l));
  return `hsl(${h}, ${s}%, ${l}%)`;
}

/**
 * `color` with its alpha scaled by `f` (0..1). Understands the two forms the graph's
 * link palette uses — `#rrggbb` (treated as alpha 1) and `rgba(...)` — and passes
 * anything else through unchanged.
 */
export function withAlpha(color: string, f: number): string {
  const rgba = color.match(/^rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)$/);
  if (rgba) {
    const a = +(parseFloat(rgba[4]) * f).toFixed(3);
    return `rgba(${rgba[1]},${rgba[2]},${rgba[3]},${a})`;
  }
  const hex = color.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
  if (hex) {
    const [r, g, b] = [hex[1], hex[2], hex[3]].map((h) => parseInt(h, 16));
    return `rgba(${r},${g},${b},${+f.toFixed(3)})`;
  }
  return color;
}
