/**
 * Sequential blue ramp for retrieval heat (the graph's "By retrieval" coloring), low → high,
 * on the dark surface (#0b0e14). Steps 600/500/400/300/150 of the dataviz reference blue;
 * validated for dark-mode ordinal use (monotone L, adjacent ΔL ≥ .06,
 * light-end contrast 2.38:1). The app is dark-only, so there is no light variant.
 */
export const HEAT_RAMP = ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#b7d3f6"] as const;

/**
 * Where a count sits between 0 and `max` on a log scale, in [0, 1]. Folder retrieval totals
 * are heavy-tailed — the vault root can hold hundreds of times the retrievals of a typical hot
 * folder — so a square-root scale piles nearly every hot folder onto the bottom ramp step;
 * log spreads them across the ramp while keeping the top steps for the truly hot.
 * Shared by color, folder size and link width so all three tell the same story.
 */
export function heatFraction(value: number, max: number): number {
  if (value <= 0 || max <= 0) return 0;
  return Math.min(1, Math.log1p(value) / Math.log1p(max));
}

/** Ramp index for a count: -1 for no hits, else 0..HEAT_RAMP.length-1 (log scaled). */
export function heatStep(value: number, max: number): number {
  if (value <= 0 || max <= 0) return -1;
  const n = HEAT_RAMP.length;
  return Math.min(n - 1, Math.floor(n * heatFraction(value, max)));
}

/** The viewer's IANA zone for bucket boundaries; "UTC" when the browser can't say. */
export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}
