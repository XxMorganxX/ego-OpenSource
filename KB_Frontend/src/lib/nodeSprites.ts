// Pre-rendered node sprites for the graph canvas.
//
// Drawing a node's glow live (ctx.shadowBlur) runs a Gaussian blur per node per frame —
// the dominant render cost once the graph has a few hundred nodes. Instead, each distinct
// (shape, color, radius) combination is rasterized ONCE into a small offscreen canvas —
// glow, fill and rim baked in — and nodes are drawn with a single drawImage.
//
// Resolution is zoom-aware so sprites stay crisp as the camera dives in: the raster
// density is devicePixelRatio × a power-of-two bucket of the current zoom. Far out, the
// bucket is small and sprites are tiny cheap bitmaps; zooming in crosses into higher
// buckets and nodes lazily re-resolve to higher-resolution sprites (cached per bucket, so
// zooming back and forth reuses them). Past SPRITE_MAX_ZOOM the caller should fall back
// to direct vector drawing — at that magnification only a handful of nodes are visible,
// and vector paths are crisp at any zoom.

export type SpriteKind = "circle" | "diamond" | "diamond-hollow";

export type NodeSprite = {
  canvas: HTMLCanvasElement;
  /** Sprite width/height in WORLD units when drawn (node + glow + rim padding). */
  worldSize: number;
};

/** Above this zoom, skip sprites and draw vector paths (bitmaps would visibly upscale). */
export const SPRITE_MAX_ZOOM = 16;
const MIN_BUCKET = 0.25;

// Glow sized relative to the node (world units), so the halo zooms with the shape and
// reads consistently at every camera height. The floor keeps small notes visibly lit.
const GLOW_RATIO = 1.2;
const GLOW_MIN = 3;
const RIM_COLOR = "rgba(255,255,255,0.35)";
// Stroke widths in CSS px AT the bucket zoom — matching the live-drawn `width / scale`
// look. Rendered at the bucket but viewed anywhere inside it, the on-screen width sways
// between 0.5–1× the target, which stays visually uniform.
const RIM_WIDTH_CSS = 1.4;
const HOLLOW_WIDTH_CSS = 1.6;
const HOLLOW_FILL_ALPHA = 0.16;

// Distinct sprites are bounded by colors × radius steps × zoom buckets. Colors are few
// (type palette / entity roles) except under the importance scheme (≤ ~281 rounded hues),
// so a hard cap with full reset keeps the cache from growing without bound; sprites are
// cheap to re-render.
const MAX_CACHE = 400;
const cache = new Map<string, NodeSprite | null>();

function dpr(): number {
  return (typeof window !== "undefined" && window.devicePixelRatio) || 1;
}

/** Quantize a camera zoom to the power-of-two resolution bucket sprites render at. */
export function zoomBucket(k: number): number {
  if (!Number.isFinite(k) || k <= 0) return 1;
  return Math.min(SPRITE_MAX_ZOOM, Math.max(MIN_BUCKET, 2 ** Math.ceil(Math.log2(k))));
}

/** Raster density (sprite pixels per world unit) for the current camera zoom. */
export function spriteDensity(k: number): number {
  return dpr() * zoomBucket(k);
}

/** Trace a diamond centered at (x, y); the caller opens/uses the path. */
export function traceDiamond(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  r: number,
) {
  ctx.moveTo(x, y - r);
  ctx.lineTo(x + r, y);
  ctx.lineTo(x, y + r);
  ctx.lineTo(x - r, y);
  ctx.closePath();
}

/**
 * The sprite for a node look at a raster density (from {@link spriteDensity}); cached.
 * Radius is quantized to ¼-world-unit steps so continuously-sized folders don't each mint
 * a sprite. Returns null when 2D rasterization is unavailable (e.g. jsdom) — callers fall
 * back to vector drawing.
 */
export function getSprite(
  kind: SpriteKind,
  color: string,
  radius: number,
  density: number,
): NodeSprite | null {
  const r = Math.max(0.5, Math.round(radius * 4) / 4);
  const key = `${kind}|${color}|${r}|${density}`;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  const sprite = renderSprite(kind, color, r, density);
  if (cache.size >= MAX_CACHE) cache.clear();
  cache.set(key, sprite);
  return sprite;
}

function renderSprite(
  kind: SpriteKind,
  color: string,
  r: number,
  density: number,
): NodeSprite | null {
  const glow = kind === "diamond-hollow" ? 0 : Math.max(GLOW_MIN, r * GLOW_RATIO);
  const strokePx = (kind === "diamond-hollow" ? HOLLOW_WIDTH_CSS : RIM_WIDTH_CSS) * dpr();
  const strokeWorld = strokePx / density;
  const sizePx = Math.max(2, Math.ceil((r + glow + strokeWorld) * 2 * density) + 2);

  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = sizePx;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  const c = sizePx / 2;
  const R = r * density;
  ctx.beginPath();
  if (kind === "circle") ctx.arc(c, c, R, 0, 2 * Math.PI);
  else traceDiamond(ctx, c, c, R);

  if (kind === "diamond-hollow") {
    // A concrete instance reads as secondary: faint fill, crisp outline, no glow.
    ctx.globalAlpha = HOLLOW_FILL_ALPHA;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.lineWidth = strokePx;
    ctx.strokeStyle = color;
    ctx.stroke();
  } else {
    ctx.shadowColor = color;
    ctx.shadowBlur = glow * density; // shadowBlur is in raster px (ignores the CTM)
    ctx.fillStyle = color;
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.lineWidth = strokePx;
    ctx.strokeStyle = RIM_COLOR;
    ctx.stroke();
  }
  return { canvas, worldSize: sizePx / density };
}
