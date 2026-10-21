import { useCallback, useEffect, useState } from "react";

import { useMediaQuery } from "./useMediaQuery";

/** One stage's dwell time at 1×. */
export const STAGE_MS = 1800;
export type Speed = 0.5 | 1 | 2;

/** The next available stage index in `dir`, or `from` when there is none. */
export function nextAvailable(available: boolean[], from: number, dir: 1 | -1): number {
  for (let i = from + dir; i >= 0 && i < available.length; i += dir) if (available[i]) return i;
  return from;
}

/** Stage index + transport for the replay overlay. Unavailable stages are never landed on. */
export function useReplayPlayer(available: boolean[]) {
  const first = Math.max(0, available.indexOf(true));
  const [index, setIndex] = useState(first);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<Speed>(1);
  // Callbacks and the timer depend on the CONTENTS of `available` (via `key`), not
  // on the array's identity, which changes every render of the overlay.
  const key = available.join(",");

  const next = useCallback(() => setIndex((i) => nextAvailable(available, i, 1)), [key]);
  const prev = useCallback(() => setIndex((i) => nextAvailable(available, i, -1)), [key]);
  const goTo = useCallback((i: number) => {
    if (available[i]) setIndex(i);
  }, [key]);
  const play = useCallback(() => {
    // Finished? Start over rather than "playing" a replay that cannot advance.
    setIndex((i) => (nextAvailable(available, i, 1) === i ? first : i));
    setPlaying(true);
  }, [key, first]);
  const pause = useCallback(() => setPlaying(false), []);

  // `useState(first)` only sees the list on mount; the overlay mounts with [] and
  // the stages load later. Re-home onto the first available stage whenever the
  // current one is not (or not yet) available.
  useEffect(() => {
    if (available[index] || !available.includes(true)) return;
    setIndex(available.indexOf(true));
  }, [key, index]);

  useEffect(() => {
    if (!playing) return;
    const n = nextAvailable(available, index, 1);
    if (n === index) {
      setPlaying(false);
      return;
    }
    const t = setTimeout(() => setIndex(n), STAGE_MS / speed);
    return () => clearTimeout(t);
  }, [playing, index, speed, key]);

  return { index, playing, speed, setSpeed, play, pause, next, prev, goTo };
}

/** OS "reduce motion" — the replay then jump-cuts between stages, no pulses or tweens. */
export function usePrefersReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}
