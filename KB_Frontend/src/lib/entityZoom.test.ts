import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { exitProgress, useEntityZoom } from "./entityZoom";

// exitProgress(k, base, exitRatio, startRatio) → 0..1 crossfade progress for the
// scroll-out exit gesture: 0 while the camera is still near the focused baseline zoom,
// 1 at (and past) the exit threshold, smooth in between.
describe("exitProgress", () => {
  const BASE = 10;
  const EXIT = 0.45;
  const START = 0.85;

  it("is 0 at the baseline zoom and above it", () => {
    expect(exitProgress(BASE, BASE, EXIT, START)).toBe(0);
    expect(exitProgress(BASE * 2, BASE, EXIT, START)).toBe(0);
  });

  it("is 0 until the ramp starts (k ≥ base·startRatio)", () => {
    expect(exitProgress(BASE * START, BASE, EXIT, START)).toBe(0);
    expect(exitProgress(BASE * (START + 0.01), BASE, EXIT, START)).toBe(0);
  });

  it("is 1 at the exit threshold and below it", () => {
    expect(exitProgress(BASE * EXIT, BASE, EXIT, START)).toBe(1);
    expect(exitProgress(BASE * EXIT * 0.5, BASE, EXIT, START)).toBe(1);
  });

  it("is strictly between 0 and 1 inside the ramp, at 0.5 at its midpoint", () => {
    const mid = BASE * ((START + EXIT) / 2);
    expect(exitProgress(mid, BASE, EXIT, START)).toBeCloseTo(0.5, 5);
    const early = exitProgress(BASE * 0.8, BASE, EXIT, START);
    expect(early).toBeGreaterThan(0);
    expect(early).toBeLessThan(0.5);
    const late = exitProgress(BASE * 0.5, BASE, EXIT, START);
    expect(late).toBeGreaterThan(0.5);
    expect(late).toBeLessThan(1);
  });

  it("is monotonic: zooming further out never loses progress", () => {
    // Ascending k (zooming back in) must never increase progress.
    let prev = 1;
    for (let k = BASE * EXIT; k <= BASE * START; k += 0.05) {
      const t = exitProgress(k, BASE, EXIT, START);
      expect(t).toBeGreaterThanOrEqual(0);
      expect(t).toBeLessThanOrEqual(prev);
      prev = t;
    }
    // spot-check ordering: further out (smaller k) means more progress
    expect(exitProgress(6, BASE, EXIT, START)).toBeGreaterThan(
      exitProgress(8, BASE, EXIT, START),
    );
  });

  it("eases at the ramp edges (smoothstep, not linear)", () => {
    // Just inside the ramp start, a linear ramp would already show ~2.5% progress;
    // smoothstep stays much flatter near the edge.
    const nearStart = exitProgress(BASE * (START - 0.01), BASE, EXIT, START);
    expect(nearStart).toBeGreaterThan(0);
    expect(nearStart).toBeLessThan(0.01);
  });

  it("degenerates to a step at the threshold when exitRatio ≥ startRatio (sparse view)", () => {
    // A sparse (one-node) class uses a lenient exitRatio of 3.0 — there is no zoom
    // range to ramp across, so progress must be a hard 0/1 step at the threshold.
    expect(exitProgress(BASE * 3.01, BASE, 3.0, START)).toBe(0);
    expect(exitProgress(BASE * 2.99, BASE, 3.0, START)).toBe(1);
  });

  it("is 0 when the baseline is missing or degenerate", () => {
    expect(exitProgress(5, null, EXIT, START)).toBe(0);
    expect(exitProgress(5, 0, EXIT, START)).toBe(0);
  });
});

// --- Hook-level: exit arming and trigger. The regression that motivated these: the hook
// used to read the visible-node count via fg.graphData?.(), a method react-force-graph
// does NOT expose on its ref — so the count was always 0, EVERY focused view counted as
// sparse, and the first wheel tick exited instantly. The count now arrives through a ref
// owned by GraphView, which actually has the data.
describe("useEntityZoom exit gesture", () => {
  // Mirrors the module's private FIT_DELAY_MS and FIT_MS + the 120ms re-arm margin in
  // fit(); if those timings change, the arm never happens and these fail loudly.
  const FIT_DELAY_MS = 60;
  const ARM_MS = 240 + 120;
  const SETTLE = FIT_DELAY_MS + ARM_MS + 10;

  function setup(visibleCount: number, settledZoom = 4) {
    const fg = {
      zoom: vi.fn(() => settledZoom),
      zoomToFit: vi.fn(),
      centerAt: vi.fn(),
    };
    const fgRef = { current: fg };
    const onSelectClass = vi.fn();
    const visibleRef = { current: () => true };
    const visibleCountRef = { current: visibleCount };
    const hook = renderHook(
      ({ cls }: { cls: string | null }) =>
        useEntityZoom(fgRef, cls, true, onSelectClass, visibleRef, visibleCountRef),
      { initialProps: { cls: null as string | null } },
    );
    // Drill in and let the focus fit settle so exit detection arms.
    act(() => {
      hook.rerender({ cls: "class-a" });
    });
    act(() => {
      vi.advanceTimersByTime(SETTLE);
    });
    return { hook, fg, onSelectClass, visibleCountRef, SETTLE };
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("does NOT exit a populated class on the first small zoom-out tick", () => {
    const { hook, onSelectClass } = setup(48);
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.87 }); // one wheel tick out
    });
    expect(onSelectClass).not.toHaveBeenCalled();
  });

  it("publishes ramp progress mid-gesture without exiting", () => {
    const { hook, onSelectClass } = setup(48);
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.65 });
    });
    expect(onSelectClass).not.toHaveBeenCalled();
    const t = hook.result.current.exitProgressRef.current;
    expect(t).toBeGreaterThan(0);
    expect(t).toBeLessThan(1);
    // backing off fades the ghosts again
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.95 });
    });
    expect(hook.result.current.exitProgressRef.current).toBe(0);
  });

  it("exits a populated class once the gesture crosses the threshold", () => {
    const { hook, onSelectClass } = setup(48);
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.44 });
    });
    expect(onSelectClass).toHaveBeenCalledWith(null);
  });

  it("exits a sparse (single-node) view on the first gesture", () => {
    const { hook, onSelectClass } = setup(1);
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.99 });
    });
    expect(onSelectClass).toHaveBeenCalledWith(null);
  });

  it("re-arms on a direct class→class switch: a sparse ratio must not leak", () => {
    // Focus a sparse class A (lenient ratio), then sidebar-switch straight to a
    // populated class B without passing through the overview. B must re-fit and
    // reclassify — otherwise A's sparse ratio makes B's first tick exit instantly.
    const { hook, onSelectClass, visibleCountRef, SETTLE } = setup(1);
    visibleCountRef.current = 48;
    act(() => {
      hook.rerender({ cls: "class-b" });
    });
    act(() => {
      vi.advanceTimersByTime(SETTLE);
    });
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.87 }); // one wheel tick out in B
    });
    expect(onSelectClass).not.toHaveBeenCalled();
  });

  it("drops mid-ramp ghost progress when the focus moves to another class", () => {
    const { hook, visibleCountRef } = setup(48);
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.65 }); // hold a mid-ramp mix on A
    });
    expect(hook.result.current.exitProgressRef.current).toBeGreaterThan(0);
    visibleCountRef.current = 30;
    act(() => {
      hook.rerender({ cls: "class-b" }); // sidebar switch — B must start un-ghosted
    });
    expect(hook.result.current.exitProgressRef.current).toBe(0);
  });

  it("clears ghost progress once the exit completes", () => {
    const { hook, SETTLE } = setup(48);
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.44 }); // trigger the exit
    });
    act(() => {
      hook.rerender({ cls: null }); // parent confirms the swap
    });
    act(() => {
      vi.advanceTimersByTime(SETTLE);
    });
    expect(hook.result.current.exitProgressRef.current).toBe(0);
  });

  it("re-arms after an exit: re-entering a class does not exit on the first tick", () => {
    const { hook, onSelectClass, SETTLE } = setup(48);
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.44 });
    });
    onSelectClass.mockClear();
    act(() => {
      hook.rerender({ cls: null });
    });
    act(() => {
      vi.advanceTimersByTime(SETTLE);
    });
    act(() => {
      hook.rerender({ cls: "class-a" }); // drill back in
    });
    act(() => {
      vi.advanceTimersByTime(SETTLE);
    });
    act(() => {
      hook.result.current.handleZoom({ k: 4 * 0.87 });
    });
    expect(onSelectClass).not.toHaveBeenCalled();
  });
});
